# Copyright 2020 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import math
import json
import zlib
import yaml

import boto3 # pylint: disable=import-error
import jinja2 # pylint: disable=import-error

periodic_template = """
- name: {{job_name}}
  cron: '{{cron}}'
  labels:
    preset-service-account: "true"
    preset-aws-ssh: "true"
    preset-aws-credential: "true"
  decorate: true
  decoration_config:
    timeout: {{job_timeout}}
  extra_refs:
  - org: kubernetes
    repo: kops
    base_ref: master
    workdir: true
    path_alias: k8s.io/kops
  spec:
    containers:
    - command:
      - runner.sh
      args:
      - bash
      - -c
      - |
        make test-e2e-install
        kubetest2 kops \\
          -v 2 \\
          --up --down \\
          --cloud-provider=aws \\
          --create-args="{{create_args}}" \\
          {%- if kops_feature_flags %}
          --env=KOPS_FEATURE_FLAGS={{kops_feature_flags}} \\
          {%- endif %}
          --kops-version-marker={{kops_deploy_url}} \\
          {%- if publish_version_marker %}
          --publish-version-marker={{publish_version_marker}} \\
          {%- endif %}
          --kubernetes-version={{k8s_deploy_url}} \\
          {%- if terraform_version %}
          --terraform-version={{terraform_version}} \\
          {%- endif %}
          {%- if validation_wait %}
          --validation-wait={{validation_wait}} \\
          {%- endif %}
          --test=kops \\
          -- \\
          --ginkgo-args="--debug" \\
          --test-args="-test.timeout={{test_timeout}} -num-nodes=0" \\
          {%- if test_package_bucket %}
          --test-package-bucket={{test_package_bucket}} \\
          {%- endif %}
          {%- if test_package_dir %}
          --test-package-dir={{test_package_dir}} \\
          {%- endif %}
          --test-package-marker={{marker}} \\
          --parallel={{test_parallelism}} \\
          {%- if focus_regex %}
          --focus-regex="{{focus_regex}}" \\
          {%- endif %}
          --skip-regex="{{skip_regex}}"
      env:
      - name: KUBE_SSH_KEY_PATH
        value: /etc/aws-ssh/aws-ssh-private
      - name: KUBE_SSH_USER
        value: {{kops_ssh_user}}
      image: gcr.io/k8s-testimages/kubekins-e2e:v20210610-c72cf72-master
      imagePullPolicy: Always
      resources:
        limits:
          memory: 3Gi
        requests:
          cpu: "2"
          memory: 3Gi
"""

presubmit_template = """
  - name: {{job_name}}
    branches:
    - {{branch}}
    {%- if run_if_changed %}
    run_if_changed: '{{run_if_changed}}'
    {%- endif %}
    always_run: {{always_run}}
    skip_report: {{skip_report}}
    labels:
      {%- if cloud == "aws" %}
      preset-service-account: "true"
      preset-aws-ssh: "true"
      preset-aws-credential: "true"
      preset-bazel-scratch-dir: "true"
      preset-bazel-remote-cache-enabled: "true"
      preset-dind-enabled: "true"
      {%- else %}
      preset-k8s-ssh: "true"
      {%- endif %}
    decorate: true
    decoration_config:
      timeout: {{job_timeout}}
    path_alias: k8s.io/kops
    spec:
      {%- if cloud == "gce" %}
      serviceAccountName: k8s-kops-test
      {%- endif %}
      containers:
      - image: gcr.io/k8s-testimages/kubekins-e2e:v20210610-c72cf72-master
        imagePullPolicy: Always
        command:
        - runner.sh
        args:
        - bash
        - -c
        - |
            make test-e2e-install
            kubetest2 kops \\
            -v 2 \\
            --up --build --down \\
            --cloud-provider={{cloud}} \\
            --create-args="{{create_args}}" \\
            {%- if kops_feature_flags %}
            --env=KOPS_FEATURE_FLAGS={{kops_feature_flags}} \\
            {%- endif %}
            --kubernetes-version={{k8s_deploy_url}} \\
            --kops-binary-path=/home/prow/go/src/k8s.io/kops/bazel-bin/cmd/kops/linux-amd64/kops \\
            {%- if terraform_version %}
            --terraform-version={{terraform_version}} \\
            {%- endif %}
            --test=kops \\
            -- \\
            --ginkgo-args="--debug" \\
            --test-args="-test.timeout={{test_timeout}} -num-nodes=0" \\
            {%- if test_package_bucket %}
            --test-package-bucket={{test_package_bucket}} \\
            {%- endif %}
            {%- if test_package_dir %}
            --test-package-dir={{test_package_dir}} \\
            {%- endif %}
            --test-package-marker={{marker}} \\
            --parallel={{test_parallelism}} \\
            {%- if focus_regex %}
            --focus-regex="{{focus_regex}}" \\
            {%- endif %}
            --skip-regex="{{skip_regex}}"
        securityContext:
          privileged: true
        env:
        - name: KUBE_SSH_KEY_PATH
          value: {{kops_ssh_key_path}}
        - name: KUBE_SSH_USER
          value: {{kops_ssh_user}}
        - name: GOPATH
          value: /home/prow/go
        resources:
          requests:
            cpu: "2"
            memory: "6Gi"
"""

# We support rapid focus on a few tests of high concern
# This should be used for temporary tests we are evaluating,
# and ideally linked to a bug, and removed once the bug is fixed
run_hourly = [
]

run_daily = [
    'kops-grid-scenario-service-account-iam',
    'kops-grid-scenario-arm64',
    'kops-grid-scenario-serial-test-for-timeout',
    'kops-grid-scenario-terraform',
]

# These are job tab names of unsupported grid combinations
skip_jobs = [
]

def simple_hash(s):
    # & 0xffffffff avoids python2/python3 compatibility
    return zlib.crc32(s.encode()) & 0xffffffff

def build_cron(key, runs_per_day):
    runs_per_week = 0
    minute = simple_hash("minutes:" + key) % 60
    hour = simple_hash("hours:" + key) % 24
    day_of_week = simple_hash("day_of_week:" + key) % 7

    if runs_per_day > 0:
        hour_denominator = 24 / runs_per_day
        hour_offset = simple_hash("hours:" + key) % hour_denominator
        return "%d %d-23/%d * * *" % (minute, hour_offset, hour_denominator), (runs_per_day * 7)

    # run Ubuntu 20.04 (Focal) jobs more frequently
    if "u2004" in key:
        runs_per_week += 7
        return "%d %d * * *" % (minute, hour), runs_per_week

    # run hotlist jobs more frequently
    if key in run_hourly:
        runs_per_week += 24 * 7
        return "%d * * * *" % (minute), runs_per_week

    if key in run_daily:
        runs_per_week += 7
        return "%d %d * * *" % (minute, hour), runs_per_week

    runs_per_week += 1
    return "%d %d * * %d" % (minute, hour, day_of_week), runs_per_week

def replace_or_remove_line(s, pattern, new_str):
    keep = []
    for line in s.split('\n'):
        if pattern in line:
            if new_str:
                line = line.replace(pattern, new_str)
                keep.append(line)
        else:
            keep.append(line)
    return '\n'.join(keep)

def should_skip_newer_k8s(k8s_version, kops_version):
    if kops_version is None:
        return False
    if k8s_version is None:
        return True
    return float(k8s_version) > float(kops_version)

def k8s_version_info(k8s_version):
    test_package_bucket = ''
    test_package_dir = ''
    if k8s_version == 'latest':
        marker = 'latest.txt'
        k8s_deploy_url = "https://storage.googleapis.com/kubernetes-release/release/latest.txt"
    elif k8s_version == 'ci':
        marker = 'latest.txt'
        k8s_deploy_url = "https://storage.googleapis.com/kubernetes-release-dev/ci/latest.txt"
        test_package_bucket = 'kubernetes-release-dev'
        test_package_dir = 'ci'
    elif k8s_version == 'stable':
        marker = 'stable.txt'
        k8s_deploy_url = "https://storage.googleapis.com/kubernetes-release/release/stable.txt"
    elif k8s_version:
        marker = f"stable-{k8s_version}.txt"
        k8s_deploy_url = f"https://storage.googleapis.com/kubernetes-release/release/stable-{k8s_version}.txt" # pylint: disable=line-too-long
    else:
        raise Exception('missing required k8s_version')
    return marker, k8s_deploy_url, test_package_bucket, test_package_dir

def create_args(kops_channel, networking, container_runtime, extra_flags, kops_image):
    args = f"--channel={kops_channel} --networking=" + networking
    if container_runtime:
        args += f" --container-runtime={container_runtime}"

    if kops_image:
        image_overridden = False
        if extra_flags:
            for arg in extra_flags:
                if "--image=" in arg:
                    image_overridden = True
                args = args + " " + arg
        if not image_overridden:
            args = f"--image='{kops_image}' {args}"
    return args.strip()

def build_skip_regex(cloud, k8s_version, networking, distro):
    regex = r'\[Slow\]|\[Serial\]|\[Disruptive\]|\[Flaky\]|\[Feature:.+\]|\[HPA\]|\[Driver:.nfs\]|Dashboard|RuntimeClass|RuntimeHandler' # pylint: disable=line-too-long
    if networking in ['kubenet', 'canal', 'weave', 'cilium', 'cilium-etcd']:
        regex += r'|Services.*rejected.*endpoints'
    if networking in ("cilium", "cilium-etcd"):
        # https://github.com/cilium/cilium/issues/10002
        regex += r'|TCP.CLOSE_WAIT'
        # https://github.com/cilium/cilium/issues/15361
        regex += r'|external.IP.is.not.assigned.to.a.node'
    elif networking != 'calico':
        regex += r'|Services.*functioning.*NodePort'
    if networking == 'kube-router':
        regex += r'|load-balancer|hairpin|affinity\stimeout|service\.kubernetes\.io|CLOSE_WAIT' # pylint: disable=line-too-long
    elif networking == 'kubenet':
        regex += r'|Services.*affinity'
    if 'arm64' in distro:
        regex += r'|Simple.pod.should.handle.in-cluster.config'

    if cloud == 'aws':
        # TODO(rifelpet): Remove once k8s tags has been created that include
        #  https://github.com/kubernetes/kubernetes/pull/101443
        if k8s_version in ('stable', '1.21'):
            regex += r'|Invalid.AWS.KMS.key'

        # TODO(rifelpet): Remove once volume limits tests have been fixed
        # https://github.com/kubernetes/kubernetes/issues/79660#issuecomment-854884112
        if k8s_version in ('latest', '1.22'):
            regex += r'|Volume.limits.should.verify.that.all.nodes.have.volume.limits'
    return regex

def latest_aws_image(owner, name):
    client = boto3.client('ec2', region_name='us-east-1')
    response = client.describe_images(
        Owners=[owner],
        Filters=[
            {
                'Name': 'name',
                'Values': [
                    name,
                ],
            },
        ],
    )
    images = {}
    for image in response['Images']:
        images[image['CreationDate']] = image['ImageLocation']
    return images[sorted(images, reverse=True)[0]]

distro_images = {
    'amzn2': latest_aws_image('137112412989', 'amzn2-ami-hvm-*-x86_64-gp2'),
    'centos7': latest_aws_image('125523088429', 'CentOS 7.*x86_64'),
    'centos8': latest_aws_image('125523088429', 'CentOS 8.*x86_64'),
    'deb9': latest_aws_image('379101102735', 'debian-stretch-hvm-x86_64-gp2-*'),
    'deb10': latest_aws_image('136693071363', 'debian-10-amd64-*'),
    'flatcar': latest_aws_image('075585003325', 'Flatcar-stable-*-hvm'),
    'rhel7': latest_aws_image('309956199498', 'RHEL-7.*_HVM_*-x86_64-0-Hourly2-GP2'),
    'rhel8': latest_aws_image('309956199498', 'RHEL-8.*_HVM-*-x86_64-0-Hourly2-GP2'),
    'u1804': latest_aws_image('099720109477', 'ubuntu/images/hvm-ssd/ubuntu-bionic-18.04-amd64-server-*'), # pylint: disable=line-too-long
    'u2004': latest_aws_image('099720109477', 'ubuntu/images/hvm-ssd/ubuntu-focal-20.04-amd64-server-*'), # pylint: disable=line-too-long
    'u2004arm64': latest_aws_image('099720109477', 'ubuntu/images/hvm-ssd/ubuntu-focal-20.04-arm64-server-*'), # pylint: disable=line-too-long
    'u2010': latest_aws_image('099720109477', 'ubuntu/images/hvm-ssd/ubuntu-groovy-20.10-amd64-server-*'), # pylint: disable=line-too-long
}

distros_ssh_user = {
    'amzn2': 'ec2-user',
    'centos7': 'centos',
    'centos8': 'centos',
    'deb9': 'admin',
    'deb10': 'admin',
    'flatcar': 'core',
    'rhel7': 'ec2-user',
    'rhel8': 'ec2-user',
    'u1804': 'ubuntu',
    'u2004': 'ubuntu',
    'u2004arm64': 'ubuntu',
    'u2010': 'ubuntu',
}

##############
# Build Test #
##############

# Returns a string representing the periodic prow job and the number of job invocations per week
def build_test(cloud='aws',
               distro='u2004',
               networking='kubenet',
               container_runtime='containerd',
               k8s_version='latest',
               kops_channel='alpha',
               kops_version=None,
               publish_version_marker=None,
               name_override=None,
               feature_flags=(),
               extra_flags=None,
               extra_dashboards=None,
               terraform_version=None,
               test_parallelism=25,
               test_timeout_minutes=60,
               skip_override=None,
               focus_regex=None,
               runs_per_day=0,
               use_new_skip_logic=False):
    # pylint: disable=too-many-statements,too-many-branches,too-many-arguments

    if kops_version is None:
        # TODO: Move to kops-ci/markers/master/ once validated
        kops_deploy_url = "https://storage.googleapis.com/kops-ci/bin/latest-ci-updown-green.txt"
    elif kops_version.startswith("https://"):
        kops_deploy_url = kops_version
        kops_version = None
    else:
        kops_deploy_url = f"https://storage.googleapis.com/kops-ci/markers/release-{kops_version}/latest-ci-updown-green.txt" # pylint: disable=line-too-long


    # https://github.com/cilium/cilium/blob/71cfb265d53b63a2be3806fb3fd4425fa36262ff/Documentation/install/system_requirements.rst#centos-foot
    if networking in ("cilium", "cilium-etcd") and distro not in ["u2004", "u2004arm64", "deb10", "rhel8"]: # pylint: disable=line-too-long
        return None
    if should_skip_newer_k8s(k8s_version, kops_version):
        return None

    kops_image = distro_images[distro]
    kops_ssh_user = distros_ssh_user[distro]
    validation_wait = '20m' if distro == 'flatcar' else None

    marker, k8s_deploy_url, test_package_bucket, test_package_dir = k8s_version_info(k8s_version)
    args = create_args(kops_channel, networking, container_runtime, extra_flags, kops_image)

    if use_new_skip_logic:
        if skip_override is not None:
            skip_regex = skip_override
        else:
            skip_regex = build_skip_regex(cloud, k8s_version, networking, distro)
    else:
        skip_regex = r'\[Slow\]|\[Serial\]|\[Disruptive\]|\[Flaky\]|\[Feature:.+\]|\[HPA\]|\[Driver:.nfs\]|Dashboard|RuntimeClass|RuntimeHandler|Services.*functioning.*NodePort|Services.*rejected.*endpoints|Services.*affinity' # pylint: disable=line-too-long
        if networking in ("cilium", "cilium-etcd"):
            # https://github.com/cilium/cilium/issues/10002
            skip_regex += r'|TCP.CLOSE_WAIT'
            # https://github.com/cilium/cilium/issues/15361
            skip_regex += r'|external.IP.is.not.assigned.to.a.node'

        if skip_override is not None:
            skip_regex = skip_override

        # TODO(rifelpet): Remove once k8s tags has been created that include
        #  https://github.com/kubernetes/kubernetes/pull/101443
        if cloud == 'aws' and k8s_version in ('stable', '1.21') and skip_regex:
            skip_regex += r'|Invalid.AWS.KMS.key'

        # TODO(rifelpet): Remove once volume limits tests have been fixed
        # https://github.com/kubernetes/kubernetes/issues/79660#issuecomment-854884112
        if cloud == 'aws' and k8s_version in ('latest', '1.22') and skip_regex:
            skip_regex += r'|Volume.limits.should.verify.that.all.nodes.have.volume.limits'

    suffix = ""
    if cloud and cloud != "aws":
        suffix += "-" + cloud
    if networking and networking != "kubenet":
        suffix += "-" + networking
    if distro:
        suffix += "-" + distro
    if k8s_version:
        suffix += "-k" + k8s_version.replace("1.", "")
    if kops_version:
        suffix += "-ko" + kops_version.replace("1.", "")
    if container_runtime:
        suffix += "-" + container_runtime

    tab = name_override or (f"kops-grid{suffix}")

    if tab in skip_jobs:
        return None
    job_name = f"e2e-{tab}"

    cron, runs_per_week = build_cron(tab, runs_per_day)

    tmpl = jinja2.Template(periodic_template)
    job = tmpl.render(
        job_name=job_name,
        cron=cron,
        kops_ssh_user=kops_ssh_user,
        create_args=args,
        k8s_deploy_url=k8s_deploy_url,
        kops_deploy_url=kops_deploy_url,
        test_parallelism=str(test_parallelism),
        job_timeout=str(test_timeout_minutes + 30) + 'm',
        test_timeout=str(test_timeout_minutes) + 'm',
        marker=marker,
        skip_regex=skip_regex,
        kops_feature_flags=','.join(feature_flags),
        terraform_version=terraform_version,
        test_package_bucket=test_package_bucket,
        test_package_dir=test_package_dir,
        focus_regex=focus_regex,
        publish_version_marker=publish_version_marker,
        validation_wait=validation_wait,
    )

    spec = {
        'cloud': cloud,
        'networking': networking,
        'distro': distro,
        'k8s_version': k8s_version,
        'kops_version': kops_version,
        'container_runtime': container_runtime,
        'kops_channel': kops_channel,
    }
    if feature_flags:
        spec['feature_flags'] = ','.join(feature_flags)
    if extra_flags:
        spec['extra_flags'] = ' '.join(extra_flags)
    jsonspec = json.dumps(spec, sort_keys=True)

    dashboards = [
        'sig-cluster-lifecycle-kops',
        'google-aws',
        'kops-kubetest2',
        f"kops-distro-{distro}",
        f"kops-k8s-{k8s_version or 'latest'}",
        f"kops-{kops_version or 'latest'}",
    ]
    if extra_dashboards:
        dashboards.extend(extra_dashboards)

    days_of_results = 90
    if runs_per_week * days_of_results > 7500:
        # testgrid has a limit on number of test runs to show for a job
        days_of_results = math.floor(7500 / runs_per_week)
    annotations = {
        'testgrid-dashboards': ', '.join(sorted(dashboards)),
        'testgrid-days-of-results': str(days_of_results),
        'testgrid-tab-name': tab,
    }
    for (k, v) in spec.items():
        annotations[f"test.kops.k8s.io/{k}"] = v or ""

    extra = yaml.dump({'annotations': annotations}, width=9999, default_flow_style=False)

    output = f"\n# {jsonspec}\n{job.strip()}\n"
    for line in extra.splitlines():
        output += f"  {line}\n"
    return output, runs_per_week

# Returns a string representing a presubmit prow job YAML
def presubmit_test(branch='master',
                   cloud='aws',
                   distro='u2004',
                   networking='kubenet',
                   container_runtime='containerd',
                   k8s_version='latest',
                   kops_channel='alpha',
                   name=None,
                   tab_name=None,
                   feature_flags=(),
                   extra_flags=None,
                   extra_dashboards=None,
                   test_parallelism=25,
                   test_timeout_minutes=60,
                   skip_override=None,
                   focus_regex=None,
                   run_if_changed=None,
                   skip_report=False,
                   always_run=False):
    # pylint: disable=too-many-statements,too-many-branches,too-many-arguments
    if cloud == 'aws':
        kops_image = distro_images[distro]
        kops_ssh_user = distros_ssh_user[distro]
        kops_ssh_key_path = '/etc/aws-ssh/aws-ssh-private'
        # TODO(rifelpet): Remove once k8s tags has been created that include
        #  https://github.com/kubernetes/kubernetes/pull/101443
        if k8s_version in ('stable', '1.21'):
            skip_override += r'|Invalid.AWS.KMS.key'

    elif cloud == 'gce':
        kops_image = None
        kops_ssh_user = 'prow'
        kops_ssh_key_path = '/etc/ssh-key-secret/ssh-private'

    marker, k8s_deploy_url, test_package_bucket, test_package_dir = k8s_version_info(k8s_version)
    args = create_args(kops_channel, networking, container_runtime, extra_flags, kops_image)

    tmpl = jinja2.Template(presubmit_template)
    job = tmpl.render(
        job_name=name,
        branch=branch,
        cloud=cloud,
        kops_ssh_key_path=kops_ssh_key_path,
        kops_ssh_user=kops_ssh_user,
        create_args=args,
        k8s_deploy_url=k8s_deploy_url,
        test_parallelism=str(test_parallelism),
        job_timeout=str(test_timeout_minutes + 30) + 'm',
        test_timeout=str(test_timeout_minutes) + 'm',
        marker=marker,
        skip_regex=skip_override,
        kops_feature_flags=','.join(feature_flags),
        test_package_bucket=test_package_bucket,
        test_package_dir=test_package_dir,
        focus_regex=focus_regex,
        run_if_changed=run_if_changed,
        skip_report='true' if skip_report else 'false',
        always_run='true' if always_run else 'false',
    )

    spec = {
        'cloud': cloud,
        'networking': networking,
        'distro': distro,
        'k8s_version': k8s_version,
        'container_runtime': container_runtime,
        'kops_channel': kops_channel,
    }
    if feature_flags:
        spec['feature_flags'] = ','.join(feature_flags)
    if extra_flags:
        spec['extra_flags'] = ' '.join(extra_flags)
    jsonspec = json.dumps(spec, sort_keys=True)

    dashboards = [
        'presubmits-kops',
        'kops-presubmits',
        'sig-cluster-lifecycle-kops',
        'kops-kubetest2',
        f"kops-distro-{distro}",
        f"kops-k8s-{k8s_version or 'latest'}",
    ]
    if extra_dashboards:
        dashboards.extend(extra_dashboards)

    annotations = {
        'testgrid-dashboards': ', '.join(sorted(dashboards)),
        'testgrid-days-of-results': '90',
        'testgrid-tab-name': tab_name,
    }
    for (k, v) in spec.items():
        annotations[f"test.kops.k8s.io/{k}"] = v or ""

    extra = yaml.dump({'annotations': annotations}, width=9999, default_flow_style=False)

    output = f"\n# {jsonspec}{job}\n"
    for line in extra.splitlines():
        output += f"    {line}\n"
    return output

####################
# Grid Definitions #
####################

networking_options = [
    'kubenet',
    'calico',
    'cilium',
    'cilium-etcd',
    'flannel',
    'kopeio',
]

distro_options = [
    'amzn2',
    'deb9',
    'deb10',
    'flatcar',
    'rhel7',
    'rhel8',
    'u1804',
    'u2004',
]

k8s_versions = [
    #"latest", # disabled until we're ready to test 1.22
    "1.19",
    "1.20",
    "1.21",
]

kops_versions = [
    None, # maps to latest
    "1.20",
    "1.21",
]

container_runtimes = [
    "docker",
    "containerd",
]

############################
# kops-periodics-grid.yaml #
############################
def generate_grid():
    results = []
    # pylint: disable=too-many-nested-blocks
    for container_runtime in container_runtimes:
        for networking in networking_options:
            for distro in distro_options:
                for k8s_version in k8s_versions:
                    for kops_version in kops_versions:
                        # https://github.com/kubernetes/kops/pull/11696
                        if kops_version is None and distro in ["deb9", "rhel7", "u1804"]:
                            continue
                        results.append(
                            build_test(cloud="aws",
                                       distro=distro,
                                       extra_dashboards=['kops-grid'],
                                       k8s_version=k8s_version,
                                       kops_version=kops_version,
                                       networking=networking,
                                       container_runtime=container_runtime)
                        )
    return filter(None, results)

#############################
# kops-periodics-misc2.yaml #
#############################
def generate_misc():
    results = [
        # A one-off scenario testing arm64
        build_test(name_override="kops-grid-scenario-arm64",
                   cloud="aws",
                   distro="u2004arm64",
                   extra_flags=["--zones=eu-central-1a",
                                "--node-size=m6g.large",
                                "--master-size=m6g.large"],
                   extra_dashboards=['kops-misc'],
                   use_new_skip_logic=True),

        # A special test for IPv6
        build_test(name_override="kops-grid-scenario-ipv6",
                   cloud="aws",
                   distro="u2004",
                   k8s_version="stable",
                   networking="calico",
                   feature_flags=["AWSIPv6",
                                  "EnableExternalCloudController",
                                  ],
                   runs_per_day=8,
                   extra_flags=['--ipv6',
                                '--override=cluster.spec.cloudControllerManager.cloudProvider=aws',
                                '--override=cluster.spec.cloudControllerManager.image=hakman/cloud-controller-manager:ipv6-1', # pylint: disable=line-too-long
                                '--override=cluster.spec.nonMasqueradeCIDR=fd00:10:96::/64',
                                '--override=cluster.spec.kubeDNS.upstreamNameservers=2620:119:53::53', # pylint: disable=line-too-long
                                '--override=cluster.spec.networking.calico.awsSrcDstCheck=Disable',
                                ],
                   extra_dashboards=['kops-misc'],
                   use_new_skip_logic=True),

        # A special test for JWKS
        build_test(name_override="kops-grid-scenario-service-account-iam",
                   cloud="aws",
                   distro="u2004",
                   feature_flags=["UseServiceAccountIAM"],
                   runs_per_day=3,
                   extra_flags=['--api-loadbalancer-type=public',
                                '--override=cluster.spec.serviceAccountIssuerDiscovery.discoveryStore=s3://k8s-kops-prow/e2e-dc69f71486-5831d.test-cncf-aws.k8s.io/discovery', # pylint: disable=line-too-long
                                '--override=cluster.spec.serviceAccountIssuerDiscovery.enableAWSOIDCProvider=true', # pylint: disable=line-too-long
                                ],
                   extra_dashboards=['kops-misc'],
                   use_new_skip_logic=True),

        # A special test for warm pool
        build_test(name_override="kops-warm-pool",
                   runs_per_day=3,
                   networking="cilium",
                   extra_flags=['--api-loadbalancer-type=public',
                                '--override=cluster.spec.warmPool.minSize=1'
                                ],
                   extra_dashboards=['kops-misc'],
                   use_new_skip_logic=True),

        # A special test for AWS Cloud-Controller-Manager
        build_test(name_override="kops-grid-scenario-aws-cloud-controller-manager",
                   cloud="aws",
                   distro="u2004",
                   k8s_version="latest",
                   runs_per_day=3,
                   feature_flags=["EnableExternalCloudController,SpecOverrideFlag"],
                   extra_flags=['--override=cluster.spec.cloudControllerManager.cloudProvider=aws'],
                   extra_dashboards=['provider-aws-cloud-provider-aws', 'kops-misc'],
                   use_new_skip_logic=True),

        # A special test for AWS Cloud-Controller-Manager and irsa
        build_test(name_override="kops-grid-scenario-aws-cloud-controller-manager-irsa",
                   cloud="aws",
                   distro="u2004",
                   k8s_version="latest",
                   runs_per_day=3,
                   feature_flags=["UseServiceAccountIAM,EnableExternalCloudController,SpecOverrideFlag"], # pylint: disable=line-too-long
                   extra_flags=['--override=cluster.spec.cloudControllerManager.cloudProvider=aws',
                                '--override=cluster.spec.serviceAccountIssuerDiscovery.discoveryStore=s3://k8s-kops-prow/kops-grid-scenario-aws-cloud-controller-manager-irsa/discovery', # pylint: disable=line-too-long
                                '--override=cluster.spec.serviceAccountIssuerDiscovery.enableAWSOIDCProvider=true'], # pylint: disable=line-too-long
                   extra_dashboards=['provider-aws-cloud-provider-aws', 'kops-misc'],
                   use_new_skip_logic=True),

        build_test(name_override="kops-grid-scenario-terraform",
                   k8s_version="1.20",
                   terraform_version="0.14.6",
                   extra_dashboards=['kops-misc'],
                   use_new_skip_logic=True),

        build_test(name_override="kops-aws-misc-ha-euwest1",
                   k8s_version="stable",
                   networking="calico",
                   kops_channel="alpha",
                   runs_per_day=8,
                   extra_flags=["--master-count=3", "--zones=eu-west-1a,eu-west-1b,eu-west-1c"],
                   extra_dashboards=["kops-misc"],
                   use_new_skip_logic=True),

        build_test(name_override="kops-aws-misc-arm64-release",
                   k8s_version="latest",
                   distro="u2004arm64",
                   networking="calico",
                   kops_channel="alpha",
                   runs_per_day=3,
                   extra_flags=["--zones=eu-central-1a",
                                "--node-size=m6g.large",
                                "--master-size=m6g.large"],
                   extra_dashboards=["kops-misc"],
                   use_new_skip_logic=True),

        build_test(name_override="kops-aws-misc-arm64-ci",
                   k8s_version="ci",
                   distro="u2004arm64",
                   networking="calico",
                   kops_channel="alpha",
                   runs_per_day=3,
                   extra_flags=["--zones=eu-central-1a",
                                "--node-size=m6g.large",
                                "--master-size=m6g.large"],
                   extra_dashboards=["kops-misc"],
                   use_new_skip_logic=True),

        build_test(name_override="kops-aws-misc-arm64-conformance",
                   k8s_version="ci",
                   distro="u2004arm64",
                   networking="calico",
                   kops_channel="alpha",
                   runs_per_day=3,
                   extra_flags=["--zones=eu-central-1a",
                                "--node-size=m6g.large",
                                "--master-size=m6g.large"],
                   skip_override=r'\[Slow\]|\[Serial\]|\[Flaky\]',
                   focus_regex=r'\[Conformance\]|\[NodeConformance\]',
                   extra_dashboards=["kops-misc"],
                   use_new_skip_logic=True),

        build_test(name_override="kops-aws-misc-amd64-conformance",
                   k8s_version="ci",
                   distro='u2004',
                   kops_channel="alpha",
                   runs_per_day=3,
                   extra_flags=["--node-size=c5.large",
                                "--master-size=c5.large"],
                   skip_override=r'\[Slow\]|\[Serial\]|\[Flaky\]',
                   focus_regex=r'\[Conformance\]|\[NodeConformance\]',
                   extra_dashboards=["kops-misc"],
                   use_new_skip_logic=True),

        build_test(name_override="kops-aws-misc-updown",
                   k8s_version="stable",
                   networking="calico",
                   distro='u2004',
                   kops_channel="alpha",
                   kops_version="https://storage.googleapis.com/kops-ci/bin/latest-ci.txt",
                   publish_version_marker="gs://kops-ci/bin/latest-ci-updown-green.txt",
                   runs_per_day=24,
                   extra_flags=["--node-size=c5.large",
                                "--master-size=c5.large"],
                   skip_override=r'',
                   focus_regex=r'\[k8s.io\]\sNetworking.*\[Conformance\]',
                   extra_dashboards=["kops-misc"],
                   use_new_skip_logic=True),

        build_test(name_override="kops-grid-scenario-cilium10-arm64",
                   cloud="aws",
                   networking="cilium",
                   distro="u2004arm64",
                   kops_channel="alpha",
                   runs_per_day=1,
                   extra_flags=["--zones=eu-central-1a",
                                "--node-size=m6g.large",
                                "--master-size=m6g.large"],
                   extra_dashboards=['kops-misc'],
                   use_new_skip_logic=True),

        build_test(name_override="kops-grid-scenario-cilium10-amd64",
                   cloud="aws",
                   networking="cilium",
                   distro="u2004",
                   kops_channel="alpha",
                   runs_per_day=1,
                   extra_flags=["--zones=eu-central-1a",
                                "--override=cluster.spec.networking.cilium.version=v1.10.0-rc2"],
                   extra_dashboards=['kops-misc'],
                   use_new_skip_logic=True),
    ]
    return results

###############################
# kops-periodics-distros.yaml #
###############################
def generate_distros():
    distros = ['debian9', 'debian10', 'ubuntu1804', 'ubuntu2004', 'centos7', 'centos8',
               'amazonlinux2', 'rhel7', 'rhel8', 'flatcar']
    results = []
    for distro in distros:
        distro_short = distro.replace('ubuntu', 'u').replace('debian', 'deb').replace('amazonlinux', 'amzn') # pylint: disable=line-too-long
        results.append(
            build_test(distro=distro_short,
                       networking='calico',
                       k8s_version='stable',
                       kops_channel='alpha',
                       name_override=f"kops-aws-distro-image{distro}",
                       extra_dashboards=['kops-distros'],
                       runs_per_day=3,
                       use_new_skip_logic=True,
                       )
        )
    # pprint.pprint(results)
    return results

#######################################
# kops-periodics-network-plugins.yaml #
#######################################
def generate_network_plugins():

    plugins = ['amazon-vpc', 'calico', 'canal', 'cilium', 'cilium-etcd', 'flannel', 'kopeio', 'kuberouter', 'weave'] # pylint: disable=line-too-long
    results = []
    for plugin in plugins:
        networking_arg = plugin.replace('amazon-vpc', 'amazonvpc').replace('kuberouter', 'kube-router') # pylint: disable=line-too-long
        results.append(
            build_test(
                k8s_version='stable',
                kops_channel='alpha',
                name_override=f"kops-aws-cni-{plugin}",
                networking=networking_arg,
                extra_flags=['--node-size=t3.large'],
                extra_dashboards=['kops-network-plugins'],
                runs_per_day=3,
                use_new_skip_logic=True
            )
        )
    return results

################################
# kops-periodics-versions.yaml #
################################
def generate_versions():
    skip_regex = r'\[Slow\]|\[Serial\]|\[Disruptive\]|\[Flaky\]|\[Feature:.+\]|\[HPA\]|\[Driver:.nfs\]|Dashboard|RuntimeClass|RuntimeHandler' # pylint: disable=line-too-long
    results = [
        build_test(
            k8s_version='ci',
            kops_channel='alpha',
            name_override='kops-aws-k8s-latest',
            networking='calico',
            extra_dashboards=['kops-versions'],
            runs_per_day=8,
            # This version marker is only used by the k/k presubmit job
            publish_version_marker='gs://kops-ci/bin/latest-ci-green.txt',
            skip_override=skip_regex
        )
    ]
    for version in ['1.21', '1.20', '1.19', '1.18', '1.17']:
        distro = 'deb9' if version == '1.17' else 'u2004'
        results.append(
            build_test(
                distro=distro,
                k8s_version=version,
                kops_channel='alpha',
                name_override=f"kops-aws-k8s-{version.replace('.', '-')}",
                networking='calico',
                extra_dashboards=['kops-versions'],
                runs_per_day=8,
                skip_override=skip_regex
            )
        )
    return results

######################
# kops-pipeline.yaml #
######################
def generate_pipeline():
    results = []
    for version in ['master', '1.21', '1.20', '1.19']:
        branch = version if version == 'master' else f"release-{version}"
        publish_version_marker = f"gs://kops-ci/markers/{branch}/latest-ci-updown-green.txt"
        kops_version = f"https://storage.googleapis.com/k8s-staging-kops/kops/releases/markers/{branch}/latest-ci.txt" # pylint: disable=line-too-long
        results.append(
            build_test(
                k8s_version=version.replace('master', 'latest'),
                kops_version=kops_version,
                kops_channel='alpha',
                name_override=f"kops-pipeline-updown-kops{version.replace('.', '')}",
                networking='calico',
                extra_dashboards=['kops-versions'],
                runs_per_day=24,
                skip_override=r'\[Slow\]|\[Serial\]',
                focus_regex=r'\[k8s.io\]\sNetworking.*\[Conformance\]',
                publish_version_marker=publish_version_marker,
                use_new_skip_logic=True
            )
        )
    return results

########################################
# kops-presubmits-network-plugins.yaml #
########################################
def generate_presubmits_network_plugins():
    plugins = {
        'amazonvpc': r'^(upup\/models\/cloudup\/resources\/addons\/networking\.amazon-vpc-routed-eni\/|pkg\/model\/(firewall|components\/kubeproxy|iam\/iam_builder).go|nodeup\/pkg\/model\/(context|kubelet).go|upup\/pkg\/fi\/cloudup\/defaults.go)', # pylint: disable=line-too-long
        'calico': r'^(upup\/models\/cloudup\/resources\/addons\/networking\.projectcalico\.org\/|pkg\/model\/(firewall.go|pki.go|iam\/iam_builder.go)|nodeup\/pkg\/model\/networking\/calico.go)', # pylint: disable=line-too-long
        'canal': r'^(upup\/models\/cloudup\/resources\/addons\/networking\.projectcalico\.org\.canal\/|nodeup\/pkg\/model\/networking\/(flannel|canal).go)', # pylint: disable=line-too-long
        'cilium': r'^(upup\/models\/cloudup\/resources\/addons\/networking\.cilium\.io\/|pkg\/model\/(firewall|components\/cilium|iam\/iam_builder).go|nodeup\/pkg\/model\/(context|networking\/cilium).go|upup\/pkg\/fi\/cloudup\/template_functions.go)', # pylint: disable=line-too-long
        'flannel': r'^(upup\/models\/cloudup\/resources\/addons\/networking\.flannel\/|nodeup\/pkg\/model\/(sysctls|networking\/flannel).go|upup\/pkg\/fi\/cloudup\/template_functions.go)', # pylint: disable=line-too-long
        'kuberouter': r'^(upup\/models\/cloudup\/resources\/addons\/networking\.kuberouter\/|upup\/pkg\/fi\/cloudup\/template_functions.go)', # pylint: disable=line-too-long
        'weave': r'^(upup\/models\/cloudup\/resources\/addons\/networking\.weave\/|upup\/pkg\/fi\/cloudup\/template_functions.go)' # pylint: disable=line-too-long
    }
    results = []
    skip_base = r'\[Slow\]|\[Serial\]|\[Disruptive\]|\[Flaky\]|\[Feature:.+\]|\[HPA\]|\[Driver:.nfs\]|Dashboard|RuntimeClass|RuntimeHandler' # pylint: disable=line-too-long
    for plugin, run_if_changed in plugins.items():
        networking_arg = plugin
        skip_regex = skip_base
        if plugin in ('cilium', 'cilium-etcd'):
            skip_regex += r'|should.set.TCP.CLOSE_WAIT'
        else:
            skip_regex += r'|Services.*functioning.*NodePort'
        if plugin in ['calico', 'canal', 'weave', 'cilium', 'cilium-etcd']:
            skip_regex += r'|Services.*rejected.*endpoints|external.IP.is.not.assigned.to.a.node|hostPort.but.different.hostIP|same.port.number.but.different.protocols' # pylint: disable=line-too-long
        if plugin == 'kuberouter':
            skip_regex += r'|load-balancer|hairpin|affinity\stimeout|service\.kubernetes\.io|CLOSE_WAIT' # pylint: disable=line-too-long
            networking_arg = 'kube-router'
        if plugin in ['canal', 'flannel']:
            skip_regex += r'|up\sand\sdown|headless|service-proxy-name'
        results.append(
            presubmit_test(
                k8s_version='stable',
                kops_channel='alpha',
                name=f"pull-kops-e2e-cni-{plugin}",
                tab_name=f"e2e-{plugin}",
                networking=networking_arg,
                extra_flags=['--node-size=t3.large'],
                extra_dashboards=['kops-network-plugins'],
                skip_override=skip_regex,
                run_if_changed=run_if_changed,
                skip_report=False,
                always_run=False,
            )
        )
    return results

############################
# kops-presubmits-e2e.yaml #
############################
def generate_presubmits_e2e():
    skip_regex = r'\[Slow\]|\[Serial\]|\[Disruptive\]|\[Flaky\]|\[Feature:.+\]|\[HPA\]|\[Driver:.nfs\]|Dashboard|RuntimeClass|RuntimeHandler' # pylint: disable=line-too-long
    jobs = [
        presubmit_test(
            container_runtime='docker',
            k8s_version='1.21',
            kops_channel='alpha',
            name='pull-kops-e2e-k8s-docker',
            tab_name='e2e-docker',
            always_run=False,
            skip_override=skip_regex,
        ),
        presubmit_test(
            k8s_version='1.21',
            kops_channel='alpha',
            name='pull-kops-e2e-kubernetes-aws',
            networking='calico',
            tab_name='e2e-containerd',
            always_run=True,
            skip_override=skip_regex,
        ),
        presubmit_test(
            k8s_version='1.21',
            kops_channel='alpha',
            name='pull-kops-e2e-k8s-containerd-ha',
            networking='calico',
            extra_flags=["--master-count=3", "--zones=eu-central-1a,eu-central-1b,eu-central-1c"],
            tab_name='e2e-containerd-ha',
            always_run=False,
            skip_override=skip_regex+'|Multi-AZ',
        ),
        presubmit_test(
            distro="u2010",
            networking='calico',
            container_runtime='crio',
            k8s_version='1.21',
            kops_channel='alpha',
            name='pull-kops-e2e-k8s-crio',
            tab_name='e2e-crio',
            always_run=False,
            skip_override=skip_regex,
        ),
        presubmit_test(
            cloud='gce',
            k8s_version='1.21',
            kops_channel='alpha',
            name='pull-kops-e2e-k8s-gce',
            networking='cilium',
            tab_name='e2e-gce',
            always_run=False,
            skip_override=r'\[Slow\]|\[Serial\]|\[Disruptive\]|\[Flaky\]|\[Feature:.+\]|\[HPA\]|\[Driver:.nfs\]|Firewall|Dashboard|RuntimeClass|RuntimeHandler|kube-dns|run.a.Pod.requesting.a.RuntimeClass|should.set.TCP.CLOSE_WAIT|Services.*rejected.*endpoints', # pylint: disable=line-too-long
            feature_flags=['GoogleCloudBucketACL'],
        ),
    ]
    for branch in ['1.21']:
        name_suffix = branch.replace('.', '-')
        jobs.append(
            presubmit_test(
                branch='release-' + branch,
                k8s_version=branch,
                kops_channel='alpha',
                name='pull-kops-e2e-kubernetes-aws-' + name_suffix,
                networking='calico',
                tab_name='e2e-' + name_suffix,
                always_run=True,
                skip_override=skip_regex,
            )
        )
    return jobs

########################
# YAML File Generation #
########################
periodics_files = {
    'kops-periodics-distros.yaml': generate_distros,
    'kops-periodics-grid.yaml': generate_grid,
    'kops-periodics-misc2.yaml': generate_misc,
    'kops-periodics-network-plugins.yaml': generate_network_plugins,
    'kops-periodics-versions.yaml': generate_versions,
    'kops-periodics-pipeline.yaml': generate_pipeline,
}

presubmits_files = {
    'kops-presubmits-network-plugins.yaml': generate_presubmits_network_plugins,
    'kops-presubmits-e2e.yaml': generate_presubmits_e2e,
}

def main():
    for filename, generate_func in periodics_files.items():
        print(f"Generating {filename}")
        output = []
        runs_per_week = 0
        job_count = 0
        for res in generate_func():
            output.append(res[0])
            runs_per_week += res[1]
            job_count += 1
        output.insert(0, "# Test jobs generated by build_jobs.py (do not manually edit)\n")
        output.insert(1, f"# {job_count} jobs, total of {runs_per_week} runs per week\n")
        output.insert(2, "periodics:\n")
        with open(filename, 'w') as fd:
            fd.write(''.join(output))
    for filename, generate_func in presubmits_files.items():
        print(f"Generating {filename}")
        output = []
        job_count = 0
        for res in generate_func():
            output.append(res)
            job_count += 1
        output.insert(0, "# Test jobs generated by build_jobs.py (do not manually edit)\n")
        output.insert(1, f"# {job_count} jobs\n")
        output.insert(2, "presubmits:\n")
        output.insert(3, "  kubernetes/kops:\n")
        with open(filename, 'w') as fd:
            fd.write(''.join(output))

if __name__ == "__main__":
    main()
