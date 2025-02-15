from collections.abc import (
    Iterable,
)
from itertools import (
    chain,
)
import json

import yaml

from azul import (
    config,
)
from azul.collections import (
    dict_merge,
)
from azul.deployment import (
    aws,
)
from azul.strings import (
    departition,
)
from azul.terraform import (
    chalice,
    emit_tf,
    vpc,
)

# This Terraform config creates a single EC2 instance with a bunch of Docker
# containers running on it:
#
#                  ╔══════════════════════════════════════════════════════════════════════════════════════════════════╗
#                  ║                                              gitlab                                              ║
#                  ║                                                                                                  ║
#                  ║  ┏━━━━━━━━━━━━━━━━━━━━━┓  ┏━━━━━━━━━━━━━━━━━━━━━━━━┓  ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓  ║
#                  ║  ┃       gitlab        ┃  ┃     gitlab-runner      ┃  ┃              gitlab-dind              ┃  ║
#                  ║  ┃  gitlab/gitlab-ce   ┃  ┃  gitlab/gitlab-runner  ┃  ┃            library/docker             ┃  ║
#                  ║  ┃                     ┃  ┃                        ┃  ┃                                       ┃  ║
#                  ║  ┃       ┌──────────┐  ┃  ┃   ┌───────────────┐    ┃  ┃         ┌────────────┐                ┃  ║
#                  ║  ┃       │  rails   │─ ╋ ▶┃   │ gitlab-runner │────╋──╋───▶ 2375│  dockerd   │docker.sock ◀─┐ ┃  ║
#         ╔═════╗  ║  ┃       └──────────┘  ┃  ┃   └───┬───────┬───┘    ┃  ┃         └────────────┘              │ ┃  ║
#         ║ ALB ║  ║  ┃       ┌──────────┐  ┃  ┃       │                ┃  ┃                                     │ ┃  ║
# ┌──▶ 443╠ ─ ─ ╬──╬──╋───▶ 80│  nginx   │  ┃  ┗━━━━━━━╋━━━━━━━╋━━━━━━━━┛  ┃      ┏━━━━━━━━━━━━━━━━━━┓           │ ┃  ║
# │       ╚═════╝  ║  ┃       └──────────┘  ┃          │   ▲               ┃      ┃    "executor"    ┃           │ ┃  ║
# │       ╔═════╗  ║  ┃       ┌──────────┐  ┃          │       │           ┃      ┃ ucsc/azul/runner ┃           │ ┃  ║
# │     22╠ ─ ─ ╬──╬──╋─▶ 2222│  gitaly  │  ┃          │   │    ─ ─ ─ ─ ─ ─┃─ ─ ─▶┃                  ┃           │ ┃  ║
# │       ║     ║  ║  ┃       └──────────┘  ┃◀─ ─ ─    │                   ┃      ┃  ┌────────────┐  ┃           │ ┃  ║
# │       ║     ║  ║  ┃                     ┃      │   │   │               ┃    ─ ╋ ─│    make    │  ┃           │ ┃  ║
# │       ║ NLB ║  ║  ┗━━━━━━━━━━━━━━━━━━━━━┛          │                   ┃   │  ┃  └────────────┘  ┃           │ ┃  ║
# │       ║     ║  ║                               │   │   │               ┃      ┃  ┌────────────┐  ┃           │ ┃  ║
# │       ║     ║  ║                  ┌─────────┐      │                   ┃   │  ┃  │   docker   │──╋───────────┤ ┃  ║
# │   2222╠ ─ ─ ╬──╬──────────────▶ 22│  sshd   │  │   │   │               ┃      ┃  └────────────┘  ┃           │ ┃  ║
# │       ╚═════╝  ║                  └─────────┘      │                   ┃   │  ┗━━━━━━━━━━━━━━━━━━┛           │ ┃  ║
# │                ║                  ┌─────────┐  │   │   │               ┃      ┏━━━━━━━━━━━━━━━━━━┓           │ ┃  ║
# │                ║   ┌─▶ docker.sock│ dockerd │      │                   ┃   │  ┃     "build"      ┃           │ ┃  ║
# │                ║   │              └─────────┘  │   │   │               ┃      ┃  ucsc/azul/dev   ┃           │ ┃  ║
# │                ║   │              ┌─────────┐      │                   ┃   │  ┃                  ┃           │ ┃  ║
# │                ║   └──────────────│ systemd │─ ┴ ─ ┼ ─ ┴ ─ ─ ─ ─ ─ ─ ─▶┃      ┃  ┌────────────┐  ┃           │ ┃  ║
# │                ║                  └─────────┘      │                   ┃   │  ┃  │    make    │  ┃           │ ┃  ║
# │                ║                                   │                   ┃    ─▶┃  └────────────┘  ┃           │ ┃  ║
# └────────────────╬───────────────────────────────────┘                   ┃      ┃  ┌────────────┐  ┃           │ ┃  ║
#                  ║                                                       ┃      ┃  │            ├──╋───────────┘ ┃  ║
#                  ║                                                       ┃ ┌────╋──│   python   │  ┃             ┃  ║
#                  ║                                                       ┃ │    ┃  │            ├ ─┃─ ┐          ┃  ║
#                  ║                                                       ┃ │    ┃  └────────────┘  ┃             ┃  ║
#                  ║                                                       ┃ │    ┗━━━━━━━━━━━━━━━━━━┛  │          ┃  ║
#                  ║                                                       ┃ │    ┏━━━━━━━━━━━━━━━━━━┓             ┃  ║
#                  ║                                                       ┃ │    ┃  elasticsearch   ┃  │          ┃  ║
#                  ║                                                       ┃ │    ┃  ┌────────────┐  ┃             ┃  ║
#                  ║                                                       ┃ └─▶ 9200│    java    │  ┃◀ ┘          ┃  ║
#                  ║                                                       ┃      ┃  └────────────┘  ┃             ┃  ║
#                  ║                                                       ┃      ┗━━━━━━━━━━━━━━━━━━┛             ┃  ║
#                  ║                                                       ┃                                       ┃  ║
#                  ║                                                       ┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛  ║
#                  ║                                                                                                  ║
#                  ╚══════════════════════════════════════════════════════════════════════════════════════════════════╝
#
#                                   ╔════════════╗  ┏━━━━━━━━━━━┓  ┌───────────┐
#                          Legend:  ║  instance  ║  ┃ container ┃  │  process  │  ───interact──▶   ─ ─ run ─ ─ ▶
#                                   ╚════════════╝  ┗━━━━━━━━━━━┛  └───────────┘
#
# The instance is fronted by two AWS load balancers:
#
# 1) an application load balancer (ALB) that terminates SSL and forwards to the
#    Gitlab web UI
#
# 2) an network load balancer that forwards port 22 to an SSH daemon in the
#    Gitlab container (for git+ssh://) and port 2222 to an SSH daemon for shell
#    access in RancherOS' `console` container.
#
# The instance itself does not have a public IP and is only reachable from the
# internet through the load balancers.
#
# The NLB's public IP is bound to ssh.gitlab.
# {dev,prod}.singlecell.gi.ucsc.edu The ALB's public IP is bound to gitlab.
# {dev,prod}.singlecell.gi.ucsc.edu To log into the instance run `ssh
# rancher@ssh.gitlab.dev.singlecell.gi.ucsc.edu -p 2222`. Your SSH key must be
# mentioned in public_key or other_public_keys below.
#
# The Gitlab web UI is at https://gitlab.{dev,prod}.singlecell.gi.ucsc.edu/. It
# is safe to destroy all resources in this TF config. You can always build them
# up again. The only golden egg is the EBS volume that's attached to the
# instance. See below under ebs_volume_name. RancherOS was chosen for the AMI
# because it has Docker pre installed and supports cloud-init user data.
#
# The container wiring is fairly complicated as it involves docker-in-docker. It
# is inspired by
#
# https://medium.com/@tonywooster/docker-in-docker-in-gitlab-runners-220caeb708ca
#
# In this setup the build container is not privileged while allowing for image
# layer caching between builds. The `elasticsearch` and `dynamodb-local`
# containers are included as examples of test fixtures launched during test
# setup. This aspect may evolve over time. It's worth noting that these fixture
# containers are siblings of the build container. When the tests are run
# locally or on Travis, the tests run on the host. The above diagram also
# glosses over the fact that there are multiple separate bridge networks
# involved. The `gitlab-dind` and `gitlab-runner` containers are attached to a
# separate bridge network. The `gitlab` container is on the default bridge
# network. IMPORTANT: There is a bug in the Terraform AWS provider (I think
# it's conflating the listeners) which causes one of the NLB listeners to be
# missing after `terraform apply`.

# The name of an EBS volume to attach to the instance. This EBS volume must
# exist, be encrypted, and be formatted with ext4. We don't manage the volume in
# Terraform because that would require formatting it once after creation. That
# can only be one after attaching it to an EC2 instance but before mounting it.
# This turns out to be difficult and risks overwriting existing data on the
# volume. We'd also have to prevent the volume from being deleted during
# `terraform destroy`.
#
# If this EBS volume does not exist you must create it with the desired size
# before running Terraform. For example:
#
# aws ec2 create-volume \
# --encrypted \
# --size 100 \
# --availability-zone "${AWS_DEFAULT_REGION}a" \
# --tag-specifications 'ResourceType=volume,Tags=[{Key=Name,Value=azul-gitlab},{Key=owner,Value=hannes@ucsc.edu}]'
#
# To then format the volume, you can then either attach it to some other Linux
# instance and format it there or use `make terraform` to create the actual
# Gitlab instance and attach the volume. For the latter you would need to ssh
# into the Gitlab instance, format `/dev/xvdf` (`/dev/nvme1n1` on newer
# instance types) and reboot the instance. For example:
#
# docker stop gitlab-runner
# docker stop gitlab
# docker stop gitlab-dind
# sudo mv /mnt/gitlab /mnt/gitlab.deleteme
# sudo mkdir /mnt/gitlab
# sudo mkfs.ext4 /dev/nvme1n1
# sudo reboot
# sudo rm -rf /mnt/gitlab.deleteme
#
# The EBS volume should be backed up (EBS snapshot) periodically. Not only does
# it contain Gitlab's data but also its config.
#
ebs_volume_name = 'azul-gitlab'

num_zones = 2  # An ALB needs at least two availability zones

# List of port forwardings by the network load balancer (NLB). The first element
# in the tuple is the port on the external interface of the NLB, the second
# element is the port on the instance the NLB forwards to.
#
nlb_ports = [(22, 2222, 'git'), (2222, 22, 'ssh')]

# The Azul Gitlab instance uses one VPC. This variable specifies the IPv4
# address block to be used by that VPC.
#
# Be sure to avoid the default Docker address pool:
#
# https://github.com/docker/libnetwork/blob/a79d3687931697244b8e03485bf7b2042f8ec6b6/ipamutils/utils.go#L10
#

cidr_offset = ['dev', 'prod', 'anvildev', 'anvilprod'].index(config.deployment_stage)

vpc_cidr = f'172.{71 + cidr_offset}.0.0/16'

vpn_subnet = f'10.{42 + cidr_offset}.0.0/16'  # can't overlap VPC CIDR and subnet mask must be <= 22 bits

# The public key of that keypair
#
administrator_key = (
    'ssh-rsa'
    ' '
    'AAAAB3NzaC1yc2EAAAADAQABAAABAQDhRBbejN2qT5+6nfpzxPTfTFuSDSiPrAyDKH+V/A9+Xw4ZT8Z3K4d0w0KlwjtRZ'
    '7shmIxkN44DY8R8LGCiybYHHVHqRNoQYqY1BkfSSP8h+eTylo4kRE4hKzs97dsBKYN1iXYXxd9yJGf6u3iR51LFijNLNN'
    '6QEsxC6PhBReye21X8KdrlOO1owG3D+BVF6Q8PxpBFTjwMLiJUe3hm/vNTrCJErtHAr6ok28BY7rj3UVbGscrnsMIpdsX'
    'OFDl5NU7tB6H9HlQ46l/W70ZSpzx8FQel9kbxcjZLinmsujuILC2bI1ev4EcdTRXo9SHo5VLPnE9J2f6StlqbBYJpbdOl'
    ' '
    'hannes@ucsc.edu'
)

operator_keys = [
    (
        'ssh-rsa'
        ' '
        'AAAAB3NzaC1yc2EAAAADAQABAAABgQCrIU25zlzHBxIdEATJZsGXvatdWuen5zlOw1uE25spQ8eNnOUfbz5fR'
        'yiQqyMNxE/dX2hCCDT1mr5Flke4uJ0FayC/l5ZC3bKYE2gnILbZBNsFuueZuDy9pRmZ+eTYs3vKXN361+loRi'
        '6ag8h/pOQCvx6oO5NrVSBse0NcEn1tk1h7C1hOf8sblW17+OO9aDQJAA7G4PJw2kBRCYYEwDNLBRy3k1wBdcK'
        'G2t2SuVh+PCpmMPA5/i/raDUqATO1H3bcRubtyGHNbAtihL5HLZK83O9fHVf/MD7il4N/9OwBNpOwvc2gi9zp'
        'ChGpbl5jA2ZfoEDEOhX4ffOD1UwmkmkoUC82BvHyAwdnqgh3Nk4qCum53TsMhXVWMW/8tr/t+AxjE3/Acwj6H'
        'VMz2j+67A0p1oaTbxBXdf00BmAYV2xPZNg8Fa2/AkQWPt4c4JJnktVjWM8/PU1h6FamyHfQ6pNmi+j6rHz9UZ'
        'e1Zt6WybGr+Tt+KifhbCnZQkg74I1uT6M='
        ' '
        'achave11@ucsc.edu'
    ),
    (
        'ssh-rsa'
        ' '
        'AAAAB3NzaC1yc2EAAAADAQABAAABAQDDPUVio1tlAstsaM2Da7QfSIv0zMU7JwjO7a/BvsWg0tXES'
        'gpL59i5QcycpYq6q7naF+N0co325e/OJ4lzi13T5xojSbh/kNETwiI+aJ9f0GxwnygcvVUpsTlH3X01fR+1xm'
        'rlGWi8AhEfbFyAFaqb2i+Whbkt9/oa3EIv4l+OSH6VSRtKRE56IvJ06hnWQ3yR57wxRBnHjiUuEBQ5I0jsye3'
        '0OE0USvjfbHqjbR9zyKCgnGuf/fY4aC+oimHu6/FSS3Q8+f5BtRrUjcYvddbAHnzrx08csztCx3s7iA5qUdhr'
        'W07wIjyG7vfB9Y70CDNsfi1Zo/Ff+IMKSzPtasXx'
        ' '
        'dsotirho@ucsc.edu'
    )
]

other_public_keys = {
    'dev': operator_keys,
    'anvildev': operator_keys,
    'anvilprod': operator_keys,
    'prod': []
}

# Note that a change to the image references here also requires updating
# azul.config.docker_images and redeploying the `shared` TF component prior to
# deploying the `gitlab` component.

clamav_image = config.docker_registry + config.docker_images['clamav']
dind_image = config.docker_registry + config.docker_images['dind']
gitlab_image = config.docker_registry + config.docker_images['gitlab']
runner_image = config.docker_registry + config.docker_images['gitlab_runner']

# For instructions on finding the latest CIS-hardened AMI, see
# OPERATOR.rst#upgrading-linux-ami
#
# CIS Amazon Linux 2 Kernel 4.14 Benchmark v2.0.0.26 - Level 1-4c096026-c6b0-440c-bd2f-6d34904e4fc6
#
ami_id = {
    'us-east-1': 'ami-0a001e75ab2e7eae2'
}

gitlab_mount = '/mnt/gitlab'


def merge(sets: Iterable[Iterable[str]]) -> Iterable[str]:
    return sorted(set(chain(*sets)))


def jw(*words):
    return ' '.join(words)


def jl(*lines):
    return '\n'.join(lines)


def qq(*words):
    return '"' + jw(*words) + '"'


emit_tf({} if config.terraform_component != 'gitlab' else {
    'data': {
        'aws_sns_topic': {
            'monitoring': {
                'name': aws.monitoring_topic_name
            }
        },
        'aws_availability_zones': {
            'available': {}
        },
        'aws_acm_certificate': {
            'gitlab_vpn': {
                'domain': 'azul-gitlab-vpn-server-' + config.deployment_stage
            }
        },
        'aws_ebs_volume': {
            'gitlab': {
                'filter': [
                    {
                        'name': 'volume-type',
                        'values': ['gp2']
                    },
                    {
                        'name': 'tag:Name',
                        'values': [ebs_volume_name]
                    }
                ],
                'most_recent': True
            }
        },
        # This Route53 zone also has to exist.
        'aws_route53_zone': {
            'gitlab': {
                'name': config.domain_name + '.',
                'private_zone': False
            }
        },
        'aws_s3_bucket': {
            'logs': {
                'bucket': aws.logs_bucket,
            }
        },
        'aws_iam_policy_document': {
            # This policy is really close to the policy size limit, if you get
            # LimitExceeded: Cannot exceed quota for PolicySize: 6144, you need
            # to strip the existing policy down by essentially replacing the
            # calls to the helper functions like allow_service() with a
            # hand-curated list of actions, potentially by starting from a copy
            # of the template output.
            'gitlab_boundary': {
                'statement': [
                    {
                        'actions': [
                            's3:ListAllMyBuckets',
                            's3:GetAccountPublicAccessBlock',
                            's3:HeadBucket'
                        ],
                        'resources': [
                            '*'
                        ]
                    },
                    {
                        'actions': [
                            's3:*'
                        ],
                        'resources': merge(
                            [
                                f'arn:aws:s3:::{bucket_name}',
                                f'arn:aws:s3:::{bucket_name}/*'
                            ] for bucket_name in (
                                [
                                    'edu-ucsc-gi-platform-hca-dev-*',
                                    'edu-ucsc-gi-singlecell-azul-*',
                                ] if 'singlecell' in config.domain_name else [
                                    'edu-ucsc-gi-platform-anvil-*',
                                    'edu-ucsc-gi-platform-anvil-*',
                                ] if 'anvil' in config.domain_name else [
                                    'edu-ucsc-gi-platform-hca-prod-*',
                                    'edu-ucsc-gi-azul-*',
                                    '*.azul.data.humancellatlas.org',
                                ]
                            )
                        )
                    },

                    {
                        'actions': [
                            'kms:ListAliases',
                            'kms:ListKeys'
                        ],
                        'resources': [
                            '*'
                        ]
                    },
                    {
                        'actions': [
                            'kms:GetKeyRotationStatus',
                            'kms:ListRetirableGrants',
                            'kms:ListResourceTags',
                            'kms:ListAliases',
                            'kms:GetKeyPolicy',
                            'kms:ListKeys',
                            'kms:ListGrants',
                            'kms:ListKeyPolicies',
                            'kms:GetParametersForImport',
                            'kms:DescribeKey',
                            'kms:GenerateMac',
                            'kms:VerifyMac'
                        ],
                        'resources': [
                            f'arn:aws:kms:{aws.region_name}:{aws.account}:key/*',
                            f'arn:aws:kms:{aws.region_name}:{aws.account}:alias/*'
                        ]
                    },

                    {
                        'actions': [
                            'sqs:ListQueues'
                        ],
                        'resources': [
                            '*'
                        ]
                    },
                    {
                        'actions': [
                            'sqs:*'
                        ],
                        'resources': [
                            f'arn:aws:sqs:{aws.region_name}:{aws.account}:azul-*'
                        ]
                    },

                    # API Gateway ARNs refer to APIs by ID so we cannot restrict
                    # to name or prefix. Even though all API Gateway ARNs start
                    # with `arn:aws:apigateway:${Region}::`, using the pattern
                    # `arn:aws:apigateway:${Region}::*` in the policy causes the
                    # CreateDomainName action to return AccessDenied for unknown
                    # reasons. Other API Gateway actions succeed with that ARN
                    # pattern in the policy.
                    {
                        'actions': [
                            'apigateway:*'
                        ],
                        'resources': [
                            '*'
                        ]
                    },

                    {
                        'actions': [
                            'es:DescribeElasticsearchInstanceTypeLimits',
                            'es:ListElasticsearchInstanceTypes',
                            'es:ListElasticsearchVersions',
                            'es:DescribeReservedElasticsearchInstances',
                            'es:ListDomainNames',
                            'es:DescribeReservedElasticsearchInstanceOfferings'
                        ],
                        'resources': [
                            '*'
                        ]
                    },

                    {
                        'actions': [
                            'es:*'
                        ],
                        'resources': [
                            f'arn:aws:es:{aws.region_name}:{aws.account}:domain/azul-*'
                        ]
                    },
                    {
                        'actions': [
                            'es:ListTags'
                        ],
                        'resources': [
                            f'arn:aws:es:{aws.region_name}:{aws.account}:domain/*'
                        ]
                    },

                    {
                        'actions': [
                            'sts:GetCallerIdentity'
                        ],
                        'resources': [
                            '*'
                        ]
                    },
                    {
                        'actions': [
                            'sts:GetFederationToken',
                            'sts:GetCallerIdentity'
                        ],
                        'resources': [
                            f'arn:aws:sts::{aws.account}:*',
                            f'arn:aws:iam::{aws.account}:*',
                            f'arn:aws:iam::{aws.account}:role/*',
                            f'arn:aws:iam::{aws.account}:user/*'
                        ]
                    },

                    {
                        # ACM ARNs refer to certificates by ID so we
                        # cannot restrict to name or prefix
                        'actions': [
                            'acm:RequestCertificate',
                            'acm:ListTagsForCertificate',
                            'acm:ListCertificates'
                        ],
                        'resources': [
                            '*'
                        ]
                    },

                    # API Gateway certs must reside in us-east-1,
                    # so we'll always add that region
                    {
                        'actions': [
                            'acm:*'
                        ],
                        'resources': [
                            f'arn:aws:acm:us-east-1:{aws.account}:certificate/*'
                        ]
                    },

                    {
                        'actions': [
                            'dynamodb:ListTables',
                            'dynamodb:DescribeTimeToLive',
                            'dynamodb:DescribeReservedCapacityOfferings',
                            'dynamodb:ListBackups',
                            'dynamodb:ListStreams',
                            'dynamodb:ListTagsOfResource',
                            'dynamodb:DescribeLimits',
                            'dynamodb:ListGlobalTables',
                            'dynamodb:DescribeReservedCapacity'
                        ],
                        'resources': [
                            '*'
                        ]
                    },
                    {
                        'actions': [
                            'dynamodb:*'
                        ],
                        'resources': [
                            f'arn:aws:dynamodb:{aws.region_name}:{aws.account}:table/azul-*',
                            f'arn:aws:dynamodb:{aws.region_name}:{aws.account}:table/azul-*/index/*'
                        ]
                    },

                    # Lambda ARNs refer to event source mappings by UUID so we
                    # cannot restrict to name or prefix
                    {
                        'actions': [
                            'lambda:CreateEventSourceMapping',
                            'lambda:ListFunctions',
                            'lambda:ListLayers',
                            'lambda:GetAccountSettings',
                            'lambda:ListEventSourceMappings',
                            'lambda:GetEventSourceMapping',
                            'lambda:ListLayerVersions',
                            'lambda:UpdateEventSourceMapping'
                        ],
                        'resources': [
                            '*'
                        ]
                    },
                    {
                        'actions': [
                            'lambda:*'
                        ],
                        'resources': [
                            f'arn:aws:lambda:{aws.region_name}:{aws.account}:event-source-mapping:*',
                            f'arn:aws:lambda:{aws.region_name}:{aws.account}:layer:azul-*',
                            f'arn:aws:lambda:{aws.region_name}:{aws.account}:function:azul-*',
                            f'arn:aws:lambda:{aws.region_name}:{aws.account}:layer:azul-*:*'
                        ]
                    },

                    *chalice.vpc_lambda_iam_policy(for_tf=True),

                    # CloudWatch does not describe any resource-level
                    # permissions
                    {
                        'actions': [
                            'cloudwatch:*'
                        ],
                        'resources': [
                            '*'
                        ]
                    },
                    {
                        'actions': [
                            'events:DescribeEventBus',
                            'events:TestEventPattern'
                        ],
                        'resources': [
                            '*'
                        ]
                    },
                    {
                        'actions': [
                            'events:*'
                        ],
                        'resources': [
                            f'arn:aws:events:{aws.region_name}:{aws.account}:rule/azul-*'
                        ]
                    },
                    # Route 53 ARNs refer to resources by ID so we cannot
                    # restrict to name or prefix
                    #
                    # FIXME: this is obviously problematic
                    {
                        'actions': [
                            'route53:*'
                        ],
                        'resources': [
                            '*'
                        ]
                    },
                    # Secret Manager ARNs refer to secrets by UUID so we cannot
                    # restrict to name or prefix
                    #
                    # FIXME: this is obviously problematic
                    #
                    {
                        'actions': [
                            'secretsmanager:CreateSecret',
                            'secretsmanager:ListSecrets',
                            'secretsmanager:GetRandomPassword'
                        ],
                        'resources': [
                            '*'
                        ]
                    },
                    {
                        'actions': [
                            'secretsmanager:*'
                        ],
                        'resources': [
                            f'arn:aws:secretsmanager:{aws.region_name}:{aws.account}:secret:*'
                        ]
                    },
                    {
                        'actions': [
                            'ssm:GetParameter'
                        ],
                        'resources': [
                            f'arn:aws:ssm:{aws.region_name}:{aws.account}:parameter/dcp/dss/*'
                        ]
                    },
                    {
                        'actions': [
                            'states:*'
                        ],
                        'resources': [
                            f'arn:aws:states:{aws.region_name}:{aws.account}:execution:azul-*:*',
                            f'arn:aws:states:{aws.region_name}:{aws.account}:stateMachine:azul-*'
                        ]
                    },
                    {
                        'actions': [
                            'states:ListStateMachines',
                            'states:CreateStateMachine'
                        ],
                        'resources': [
                            '*'
                        ]
                    },

                    # CloudFront uses identifiers in most if its ARNs, not
                    # names. The identifiers are random so we can't easily use
                    # the ARNs that reference them in policies.
                    #
                    # FIXME: Tighten GitLab security boundary
                    #        https://github.com/DataBiosphere/azul/issues/4207
                    {
                        'actions': [
                            'cloudfront:*'
                        ],
                        'resources': [
                            '*'
                        ]
                    },

                    # CloudWatch Logs
                    #
                    # FIXME: Tighten GitLab security boundary
                    #        https://github.com/DataBiosphere/azul/issues/4207
                    {
                        'actions': [
                            'logs:*'
                        ],
                        'resources': [
                            '*'
                        ]
                    },

                    # WAFv2
                    {
                        'actions': [
                            'wafv2:*'
                        ],
                        'resources': [
                            '*'
                        ]
                    }
                ]
            },
            'gitlab_iam': {
                'statement': [
                    # Let Gitlab manage roles as long as they specify the
                    # permissions boundary This prevents privilege escalation.
                    {
                        'actions': [
                            'iam:CreateRole',
                            'iam:TagRole',
                            'iam:UntagRole',
                            'iam:PutRolePolicy',
                            'iam:DeleteRolePolicy',
                            'iam:AttachRolePolicy',
                            'iam:DetachRolePolicy',
                            'iam:PutRolePermissionsBoundary'
                        ],
                        'resources': [
                            f'arn:aws:iam::{aws.account}:role/azul-*'
                        ],
                        'condition': {
                            'test': 'StringEquals',
                            'variable': 'iam:PermissionsBoundary',
                            'values': [
                                f'arn:aws:iam::{aws.account}:policy/azul-boundary'
                            ]
                        }
                    },
                    {
                        'actions': [
                            'iam:CreateServiceLinkedRole'
                        ],
                        'resources': [
                            f'arn:aws:iam::{aws.account}'
                            ':role'
                            '/aws-service-role'
                            '/ops.apigateway.amazonaws.com'
                            '/AWSServiceRoleForAPIGateway',
                        ]
                    },
                    {
                        'actions': [
                            'iam:UpdateAssumeRolePolicy',
                            'iam:TagRole',
                            'iam:DeleteRole',
                            'iam:PassRole'  # FIXME: consider iam:PassedToService condition
                        ],
                        'resources': [
                            f'arn:aws:iam::{aws.account}:role/azul-*'
                        ]
                    },
                    {
                        'actions': [
                            'iam:GetServiceLinkedRoleDeletionStatus',
                            'iam:ListAttachedGroupPolicies',
                            'iam:ListSigningCertificates',
                            'iam:ListUsers',
                            'iam:GetPolicyVersion',
                            'iam:GetLoginProfile',
                            'iam:ListPolicies',
                            'iam:GetUser',
                            'iam:GetSSHPublicKey',
                            'iam:GetPolicy',
                            'iam:ListInstanceProfiles',
                            'iam:GenerateCredentialReport',
                            'iam:ListMFADevices',
                            'iam:ListRoles',
                            'iam:SimulateCustomPolicy',
                            'iam:ListUserPolicies',
                            'iam:GetContextKeysForCustomPolicy',
                            'iam:GetServiceLastAccessedDetails',
                            'iam:GetCredentialReport',
                            'iam:ListServerCertificates',
                            'iam:GetOpenIDConnectProvider',
                            'iam:ListVirtualMFADevices',
                            'iam:ListPolicyVersions',
                            'iam:GetInstanceProfile',
                            'iam:ListRolePolicies',
                            'iam:ListAttachedRolePolicies',
                            'iam:GetAccountAuthorizationDetails',
                            'iam:GetGroupPolicy',
                            'iam:GetRole',
                            'iam:SimulatePrincipalPolicy',
                            'iam:GetContextKeysForPrincipalPolicy',
                            'iam:GetServiceLastAccessedDetailsWithEntities',
                            'iam:GetAccessKeyLastUsed',
                            'iam:ListGroupPolicies',
                            'iam:GetGroup',
                            'iam:ListPoliciesGrantingServiceAccess',
                            'iam:GetUserPolicy',
                            'iam:ListAttachedUserPolicies',
                            'iam:ListRoleTags',
                            'iam:GenerateServiceLastAccessedDetails',
                            'iam:GetSAMLProvider',
                            'iam:ListGroups',
                            'iam:ListOpenIDConnectProviders',
                            'iam:ListServiceSpecificCredentials',
                            'iam:ListSSHPublicKeys',
                            'iam:ListGroupsForUser',
                            'iam:GetServerCertificate',
                            'iam:ListEntitiesForPolicy',
                            'iam:ListAccessKeys',
                            'iam:ListAccountAliases',
                            'iam:GetAccountPasswordPolicy',
                            'iam:ListUserTags',
                            'iam:ListInstanceProfilesForRole',
                            'iam:ListSAMLProviders',
                            'iam:GetAccountSummary',
                            'iam:GetRolePolicy'
                        ],
                        'resources': [
                            '*'
                        ]
                    },
                    *(
                        # Permissions required to deploy Data Browser and
                        # Portal
                        [
                            {
                                'actions': [
                                    's3:*'
                                ],
                                'resources': [
                                    # Data Portal Dev
                                    'arn:aws:s3:::dev.singlecell.gi.ucsc.edu/*',
                                    'arn:aws:s3:::dev.singlecell.gi.ucsc.edu',
                                    # Data Browser Dev
                                    'arn:aws:s3:::dev.explore.singlecell.gi.ucsc.edu/*',
                                    'arn:aws:s3:::dev.explore.singlecell.gi.ucsc.edu',
                                    # Data Portal UX-Dev
                                    'arn:aws:s3:::ux-dev.singlecell.gi.ucsc.edu/*',
                                    'arn:aws:s3:::ux-dev.singlecell.gi.ucsc.edu',
                                    # Data Browser UX-Dev
                                    'arn:aws:s3:::ux-dev.explore.singlecell.gi.ucsc.edu/*',
                                    'arn:aws:s3:::ux-dev.explore.singlecell.gi.ucsc.edu',
                                    # Lungmap Data Portal Dev
                                    'arn:aws:s3:::data-browser.dev.lungmap.net/*',
                                    'arn:aws:s3:::data-browser.dev.lungmap.net',
                                    # Lungmap Data Browser Dev
                                    'arn:aws:s3:::dev.explore.lungmap.net/*',
                                    'arn:aws:s3:::dev.explore.lungmap.net'
                                ]
                            }
                        ] if config.deployment_stage == 'dev' else [
                            {
                                'actions': [
                                    's3:*'
                                ],
                                'resources': [
                                    # HCA Data Portal Prod
                                    'arn:aws:s3:::org-humancellatlas-data-portal-dcp2-prod/*',
                                    'arn:aws:s3:::org-humancellatlas-data-portal-dcp2-prod',
                                    # HCA Data Browser Prod
                                    'arn:aws:s3:::org-humancellatlas-data-browser-dcp2-prod/*',
                                    'arn:aws:s3:::org-humancellatlas-data-browser-dcp2-prod',
                                    # Lungmap Data Browser Prod
                                    'arn:aws:s3:::data-browser.lungmap.net/*',
                                    'arn:aws:s3:::data-browser.lungmap.net',
                                    # Lungmap Data Portal Prod
                                    'arn:aws:s3:::data-browser.explore.lungmap.net/*',
                                    'arn:aws:s3:::data-browser.explore.lungmap.net'
                                ]
                            }
                        ] if config.deployment_stage == 'prod' else [
                            # anvildev and anvilprod already follow the bucket
                            # naming convention and is covered by the S3
                            # permissions in the boundary.
                        ]
                    ),
                    # Manage VPN infrastructure for private API
                    # FIXME: Tighten GitLab security boundary
                    #        https://github.com/DataBiosphere/azul/issues/4207
                    {
                        'actions': [
                            'ec2:*'
                        ],
                        'resources': [
                            '*'
                        ]
                    },
                    {
                        'actions': [
                            'elasticloadbalancing:*'
                        ],
                        'resources': [
                            '*'
                        ]
                    },

                    # SNS
                    {
                        'actions': [
                            'sns:*'
                        ],
                        'resources': [
                            f'arn:aws:sns:{aws.region_name}:{aws.account}:azul-*'
                        ]
                    },
                    # Restricting the topic name prevents the SNS topic from
                    # being used in data blocks.
                    {
                        'actions': [
                            'sns:ListTopics'
                        ],
                        'resources': [
                            f'arn:aws:sns:{aws.region_name}:{aws.account}:*'
                        ]
                    },

                    # FedRAMP inventory
                    {
                        'actions': [
                            'config:ListDiscoveredResources',
                            'config:BatchGetResourceConfig'
                        ],
                        'resources': [
                            '*'
                        ]
                    },
                    {
                        'actions': [
                            'ecr:BatchCheckLayerAvailability',
                            'ecr:BatchGet*',
                            'ecr:Describe*',
                            'ecr:Get*',
                            'ecr:List*'
                        ],
                        'resources': [
                            '*'
                        ]
                    },
                    {
                        'actions': [
                            'logs:CreateLogGroup',
                            'logs:CreateLogStream',
                            'logs:PutLogEvents'
                        ],
                        'resources': [
                            'arn:aws:logs:*:*:*'
                        ]
                    },

                    # KMS writes (reads are granted by boundary policy)
                    {
                        'actions': [
                            'kms:CreateKey'
                        ],
                        'resources': [
                            '*'
                        ]
                    },
                    {
                        'actions': [
                            'kms:CreateAlias',
                            'kms:UpdateAlias',
                            'kms:UpdateKeyDescription',
                            'kms:ScheduleKeyDeletion',
                            'kms:DeleteAlias',
                            'kms:TagResource',
                            'kms:UntagResource'
                        ],
                        'resources': [
                            f'arn:aws:kms:{aws.region_name}:{aws.account}:key/*',
                            f'arn:aws:kms:{aws.region_name}:{aws.account}:alias/*'
                        ]
                    }
                ]
            }
        },
    },
    'resource': {
        'aws_vpc': {
            'gitlab': {
                'cidr_block': vpc_cidr
            }
        },
        'aws_subnet': {
            # A public and a private subnet per availability zone
            f'gitlab_{vpc.subnet_name(public)}_{zone}': {
                'availability_zone': f'${{data.aws_availability_zones.available.names[{zone}]}}',
                'cidr_block': f'${{cidrsubnet(aws_vpc.gitlab.cidr_block, 8, {vpc.subnet_number(zone, public)})}}',
                'map_public_ip_on_launch': public,
                'vpc_id': '${aws_vpc.gitlab.id}'
            }
            for public in (False, True)
            for zone in range(num_zones)
        },
        'aws_internet_gateway': {
            'gitlab': {
                'vpc_id': '${aws_vpc.gitlab.id}'
            }
        },
        'aws_route': {
            'gitlab': {
                'destination_cidr_block': '0.0.0.0/0',
                'gateway_id': '${aws_internet_gateway.gitlab.id}',
                'route_table_id': '${aws_vpc.gitlab.main_route_table_id}'
            }
        },
        'aws_eip': {
            f'gitlab_{zone}': {
                'depends_on': [
                    'aws_internet_gateway.gitlab'
                ],
                'domain': 'vpc'
            }
            for zone in range(num_zones)
        },
        'aws_nat_gateway': {
            f'gitlab_{zone}': {
                'allocation_id': f'${{aws_eip.gitlab_{zone}.id}}',
                'subnet_id': f'${{aws_subnet.gitlab_public_{zone}.id}}'
            }
            for zone in range(num_zones)
        },
        'aws_route_table': {
            f'gitlab_{zone}': {
                'route': [
                    {
                        'cidr_block': '0.0.0.0/0',
                        'nat_gateway_id': f'${{aws_nat_gateway.gitlab_{zone}.id}}',
                        'egress_only_gateway_id': None,
                        'gateway_id': None,
                        'instance_id': None,
                        'ipv6_cidr_block': None,
                        'network_interface_id': None,
                        'transit_gateway_id': None,
                        'vpc_peering_connection_id': None,
                        'carrier_gateway_id': None,
                        'destination_prefix_list_id': None,
                        'local_gateway_id': None,
                        'vpc_endpoint_id': None,
                        'core_network_arn': None,
                    }
                ],
                'vpc_id': '${aws_vpc.gitlab.id}'
            }
            for zone in range(num_zones)
        },
        'aws_route_table_association': {
            f'gitlab_{zone}': {
                'route_table_id': f'${{aws_route_table.gitlab_{zone}.id}}',
                'subnet_id': f'${{aws_subnet.gitlab_private_{zone}.id}}'
            }
            for zone in range(num_zones)
        },
        'aws_default_security_group': {
            'gitlab': {
                'vpc_id': '${aws_vpc.gitlab.id}',
                'egress': [],
                'ingress': []
            }
        },
        'aws_security_group': {
            'gitlab_vpn': {
                'name': 'azul-gitlab-vpn',
                'vpc_id': '${aws_vpc.gitlab.id}',
                'egress': [
                    vpc.security_rule(description='Any traffic to the VPC',
                                      cidr_blocks=['${aws_vpc.gitlab.cidr_block}'],
                                      protocol=-1,
                                      from_port=0,
                                      to_port=0),
                    vpc.security_rule(description='ICMP for PMTUD',
                                      cidr_blocks=['0.0.0.0/0'],
                                      protocol='icmp',
                                      from_port=3,  # Destination Unreachable
                                      to_port=4)  # Fragmentation required DF-flag set
                ],
                'ingress': [
                    vpc.security_rule(description='Any traffic from the VPC',
                                      cidr_blocks=['${aws_vpc.gitlab.cidr_block}'],
                                      protocol=-1,
                                      from_port=0,
                                      to_port=0),
                    vpc.security_rule(description='ICMP for PMTUD',
                                      cidr_blocks=['0.0.0.0/0'],
                                      protocol='icmp',
                                      from_port=3,  # Destination Unreachable
                                      to_port=4)  # Fragmentation required DF-flag set
                ]
            },
            'gitlab_alb': {
                'name': 'azul-gitlab-alb',
                'vpc_id': '${aws_vpc.gitlab.id}',
                'egress': [
                    vpc.security_rule(description='Any traffic to the VPC',
                                      cidr_blocks=['${aws_vpc.gitlab.cidr_block}'],
                                      protocol=-1,
                                      from_port=0,
                                      to_port=0),
                    vpc.security_rule(description='ICMP for PMTUD',
                                      cidr_blocks=['0.0.0.0/0'],
                                      protocol='icmp',
                                      from_port=3,  # Destination Unreachable
                                      to_port=4)  # Fragmentation required DF-flag set
                ],
                'ingress': [
                    vpc.security_rule(description='HTTPS from the VPC',
                                      cidr_blocks=['${aws_vpc.gitlab.cidr_block}'],
                                      protocol='tcp',
                                      from_port=443,
                                      to_port=443),
                    vpc.security_rule(description='ICMP for PMTUD',
                                      cidr_blocks=['0.0.0.0/0'],
                                      protocol='icmp',
                                      from_port=3,  # Destination Unreachable
                                      to_port=4)  # Fragmentation required DF-flag set

                ]
            },
            'gitlab': {
                'name': 'azul-gitlab',
                'vpc_id': '${aws_vpc.gitlab.id}',
                'egress': [
                    vpc.security_rule(description='Any traffic to anywhere (to be routed by NAT Gateway)',
                                      cidr_blocks=['0.0.0.0/0'],
                                      protocol=-1,
                                      from_port=0,
                                      to_port=0),
                    # VXLAN for AWS Traffic Capture to a target in the same SG
                    # In a nutshell, start target instance in this SG, set up
                    # mirroring target for instance, set up mirroring session
                    # for source. Then on target instance:
                    #
                    # sudo ip link add vxlan0 type vxlan id <VNI from session> dev eth0 local 10.0.0.207 dstport 4789
                    # sudo sysctl net.ipv6.conf.vxlan0.disable_ipv6=1
                    # sudo ip link set vxlan0 up
                    # sudo tcpdump -i vxlan0 -w /tmp/gitlab.pcap
                    # OR
                    # sudo tcpdump -i vxlan0 -w - | tee /tmp/gitlab.pcap | tcpdump -r -
                    vpc.security_rule(description='VXLAN for AWS Traffic Capture to a target in the same SG',
                                      self=True,
                                      protocol='udp',
                                      from_port=4789,
                                      to_port=4789),
                    vpc.security_rule(description='ICMP for PMTUD',
                                      cidr_blocks=['0.0.0.0/0'],
                                      protocol='icmp',
                                      from_port=3,  # Destination Unreachable
                                      to_port=4)  # Fragmentation required DF-flag set
                ],
                'ingress': [
                    vpc.security_rule(description='HTTP from VPC',
                                      cidr_blocks=['${aws_vpc.gitlab.cidr_block}'],
                                      protocol='tcp',
                                      from_port=80,
                                      to_port=80),
                    *(
                        vpc.security_rule(description=f'SSH for {name} from VPC',
                                          cidr_blocks=['${aws_vpc.gitlab.cidr_block}'],
                                          protocol='tcp',
                                          from_port=int_port,
                                          to_port=int_port)
                        for ext_port, int_port, name in nlb_ports
                    ),
                    vpc.security_rule(description='VXLAN for AWS Traffic Capture to a target in the same SG',
                                      self=True,
                                      protocol='udp',
                                      from_port=4789,
                                      to_port=4789),
                    vpc.security_rule(description='ICMP for PMTUD',
                                      cidr_blocks=['0.0.0.0/0'],
                                      protocol='icmp',
                                      from_port=3,  # Destination Unreachable
                                      to_port=4)  # Fragmentation required DF-flag set
                ]
            }
        },
        'aws_cloudwatch_log_group': {
            'gitlab_vpn': {
                'name': '/aws/vpn/azul-gitlab',
                'retention_in_days': config.audit_log_retention_days,
            },
            'gitlab_vpc': {
                'name': '/aws/vpc/azul-gitlab',
                'retention_in_days': config.audit_log_retention_days,
            },
            'gitlab_cwagent': {
                'name': '/aws/cwagent/azul-gitlab',
                'retention_in_days': config.audit_log_retention_days,
            }
        },
        'aws_flow_log': {
            'gitlab': {
                'iam_role_arn': '${aws_iam_role.gitlab_vpc.arn}',
                'log_destination': '${aws_cloudwatch_log_group.gitlab_vpc.arn}',
                'log_destination_type': 'cloud-watch-logs',
                'traffic_type': 'ALL',
                'vpc_id': '${aws_vpc.gitlab.id}',
            }
        },
        'aws_ec2_client_vpn_endpoint': {
            'gitlab': {
                'client_cidr_block': vpn_subnet,
                'security_group_ids': ['${aws_security_group.gitlab_vpn.id}'],
                'server_certificate_arn': '${data.aws_acm_certificate.gitlab_vpn.arn}',
                'transport_protocol': 'udp',
                'split_tunnel': True,
                'authentication_options': {
                    'type': 'certificate-authentication',
                    'root_certificate_chain_arn': '${data.aws_acm_certificate.gitlab_vpn.arn}'
                },
                'session_timeout_hours': 8,
                'vpc_id': '${aws_vpc.gitlab.id}',
                'connection_log_options': {
                    'enabled': True,
                    'cloudwatch_log_group': '${aws_cloudwatch_log_group.gitlab_vpn.name}'
                }
            }
        },
        'aws_ec2_client_vpn_network_association': {
            f'gitlab_{zone}': {
                'client_vpn_endpoint_id': '${aws_ec2_client_vpn_endpoint.gitlab.id}',
                'subnet_id': f'${{aws_subnet.gitlab_public_{zone}.id}}'
            }
            for zone in range(num_zones)
        },
        'aws_ec2_client_vpn_authorization_rule': {
            'gitlab': {
                'client_vpn_endpoint_id': '${aws_ec2_client_vpn_endpoint.gitlab.id}',
                'target_network_cidr': '${aws_vpc.gitlab.cidr_block}',
                'authorize_all_groups': True
            }
        },
        'aws_lb': {
            # Add an NLB so we can have a Route 53 alias record pointing at it
            'gitlab_nlb': {
                'name': 'azul-gitlab-nlb',
                'load_balancer_type': 'network',
                'internal': 'true',
                'subnets': [
                    f'${{aws_subnet.gitlab_public_{zone}.id}}' for zone in range(num_zones)
                ]
            },
            # Add an ALB for the same reason and for terminating TLS
            'gitlab_alb': {
                'name': 'azul-gitlab-alb',
                'load_balancer_type': 'application',
                'internal': 'true',
                'subnets': [
                    f'${{aws_subnet.gitlab_public_{zone}.id}}' for zone in range(num_zones)
                ],
                'security_groups': [
                    '${aws_security_group.gitlab_alb.id}'
                ],
                'access_logs': [
                    {
                        'bucket': '${data.aws_s3_bucket.logs.id}',
                        'prefix': config.alb_access_log_path_prefix('gitlab'),
                        'enabled': True
                    }
                ]
            }
        },
        'aws_lb_listener': {
            **(
                {
                    'gitlab_' + name: {
                        'port': ext_port,
                        'protocol': 'TCP',
                        'default_action': [
                            {
                                'target_group_arn': '${aws_lb_target_group.gitlab_' + name + '.id}',
                                'type': 'forward'
                            }
                        ],
                        'load_balancer_arn': '${aws_lb.gitlab_nlb.id}'
                    }
                    for ext_port, int_port, name in nlb_ports
                }
            ),
            'gitlab_http': {
                'port': 443,
                'protocol': 'HTTPS',
                'ssl_policy': 'ELBSecurityPolicy-FS-1-2-Res-2019-08',
                'certificate_arn': '${aws_acm_certificate.gitlab.arn}',
                'default_action': [
                    {
                        'target_group_arn': '${aws_lb_target_group.gitlab_http.id}',
                        'type': 'forward'
                    }
                ],
                'load_balancer_arn': '${aws_lb.gitlab_alb.id}'
            }
        },
        'aws_lb_target_group': {
            **(
                {
                    'gitlab_' + name: {
                        'name': 'azul-gitlab-' + name,
                        'port': int_port,
                        'protocol': 'TCP',
                        # A target type of `instance` preserves the source IP in
                        # packets forwarded by the NLB. Any security group
                        # guarding this traffic must allow ingress not from the
                        # NLB's internal IP but from the original source IP.
                        'target_type': 'instance',
                        'vpc_id': '${aws_vpc.gitlab.id}'
                    }
                    for ext_port, int_port, name in nlb_ports
                }
            ),
            'gitlab_http': {
                'name': 'azul-gitlab-http',
                'port': 80,
                'protocol': 'HTTP',
                'target_type': 'instance',
                'vpc_id': '${aws_vpc.gitlab.id}',
                'health_check': {
                    'protocol': 'HTTP',
                    'path': '/',
                    'port': 'traffic-port',
                    'healthy_threshold': 5,
                    'unhealthy_threshold': 2,
                    'timeout': 5,
                    'interval': 30,
                    'matcher': '302'
                }
            }
        },
        'aws_lb_target_group_attachment': {
            **(
                {
                    'gitlab_' + name: {
                        'target_group_arn': '${aws_lb_target_group.gitlab_' + name + '.arn}',
                        'target_id': '${aws_instance.gitlab.id}'
                    }
                    for ext_port, int_port, name in nlb_ports
                }
            ),
            'gitlab_http': {
                'target_group_arn': '${aws_lb_target_group.gitlab_http.arn}',
                'target_id': '${aws_instance.gitlab.id}'
            }
        },
        'aws_acm_certificate': {
            'gitlab': {
                'domain_name': '${aws_route53_record.gitlab.name}',
                'subject_alternative_names': ['${aws_route53_record.gitlab_docker.name}'],
                'validation_method': 'DNS',
                'lifecycle': {
                    'create_before_destroy': True
                }
            }
        },
        'aws_acm_certificate_validation': {
            'gitlab': {
                'certificate_arn': '${aws_acm_certificate.gitlab.arn}',
                'validation_record_fqdns': '${[for r in aws_route53_record.gitlab_validation : r.fqdn]}',
            }
        },
        'aws_route53_record': {
            'gitlab_validation': {
                # The double curlies are not a mistake. This is not an f-string,
                # it's a TF expression containing a dictiona
                'for_each': '${{for o in aws_acm_certificate.gitlab.domain_validation_options : o.domain_name => o}}',
                'name': '${each.value.resource_record_name}',
                'type': '${each.value.resource_record_type}',
                'zone_id': '${data.aws_route53_zone.gitlab.id}',
                'records': [
                    '${each.value.resource_record_value}',
                ],
                'ttl': 60
            },
            **dict_merge(
                {
                    departition('gitlab', '_', subdomain): {
                        'zone_id': '${data.aws_route53_zone.gitlab.id}',
                        'name': departition(subdomain, '.', f'gitlab.{config.domain_name}'),
                        'type': 'A',
                        'alias': {
                            'name': '${aws_lb.gitlab_alb.dns_name}',
                            'zone_id': '${aws_lb.gitlab_alb.zone_id}',
                            'evaluate_target_health': False
                        }
                    }
                }
                for subdomain in [None, 'docker']
            ),
            'gitlab_ssh': {
                'zone_id': '${data.aws_route53_zone.gitlab.id}',
                'name': f'ssh.gitlab.{config.domain_name}',
                'type': 'A',
                'alias': {
                    'name': '${aws_lb.gitlab_nlb.dns_name}',
                    'zone_id': '${aws_lb.gitlab_nlb.zone_id}',
                    'evaluate_target_health': False
                }
            }
        },
        'aws_network_interface': {
            'gitlab': {
                'subnet_id': '${aws_subnet.gitlab_private_0.id}',
                'security_groups': [
                    '${aws_security_group.gitlab.id}'
                ]
            }
        },
        'aws_volume_attachment': {
            'gitlab': {
                'device_name': '/dev/sdf',
                'volume_id': '${data.aws_ebs_volume.gitlab.id}',
                'instance_id': '${aws_instance.gitlab.id}',
                'provisioner': {
                    'local-exec': {
                        'when': 'destroy',
                        'command': 'aws ec2 stop-instances --instance-ids ${self.instance_id}'
                                   ' && aws ec2 wait instance-stopped --instance-ids ${self.instance_id}'
                    }
                }
            }
        },
        'aws_key_pair': {
            'gitlab': {
                'key_name': 'azul-gitlab',
                'public_key': administrator_key
            }
        },
        'aws_iam_role': {
            'gitlab': {
                'name': 'azul-gitlab',
                'path': '/',
                'assume_role_policy': json.dumps({
                    'Version': '2012-10-17',
                    'Statement': [
                        {
                            'Action': 'sts:AssumeRole',
                            'Principal': {
                                'Service': 'ec2.amazonaws.com'
                            },
                            'Effect': 'Allow',
                            'Sid': ''
                        }
                    ]
                })
            },
            'gitlab_vpc': {
                'name': 'azul-gitlab_vpc',
                'assume_role_policy': json.dumps({
                    'Version': '2012-10-17',
                    'Statement': [
                        {
                            'Action': 'sts:AssumeRole',
                            'Principal': {
                                'Service': 'vpc-flow-logs.amazonaws.com'
                            },
                            'Effect': 'Allow'
                        }
                    ]
                })
            }
        },
        'aws_iam_instance_profile': {
            'gitlab': {
                'name': 'azul-gitlab',
                'role': '${aws_iam_role.gitlab.name}',
            }
        },
        'aws_iam_policy': {
            'gitlab_iam': {
                'name': 'azul-gitlab-iam',
                'path': '/',
                'policy': '${data.aws_iam_policy_document.gitlab_iam.json}'
            },
            'gitlab_boundary': {
                'name': config.permissions_boundary_name,
                'path': '/',
                'policy': '${data.aws_iam_policy_document.gitlab_boundary.json}'
            },
            'gitlab_vpc': {
                'name': 'azul-gitlab_vpc',
                'policy': json.dumps({
                    'Version': '2012-10-17',
                    'Statement': [
                        {
                            'Effect': 'Allow',
                            'Action': [
                                'logs:CreateLogGroup',
                                'logs:CreateLogStream',
                                'logs:PutLogEvents',
                                'logs:DescribeLogGroups',
                                'logs:DescribeLogStreams'
                            ],
                            'Resource': '*'
                        }
                    ]
                })
            },
        },
        'aws_iam_service_linked_role': {
            'gitlab_ssm': {
                'aws_service_name': 'ssm.amazonaws.com',
            }
        },
        'aws_iam_role_policy_attachment': {
            'gitlab_iam': {
                'role': '${aws_iam_role.gitlab.name}',
                'policy_arn': '${aws_iam_policy.gitlab_iam.arn}'
            },
            'gitlab_ssm': {
                'role': '${aws_iam_role.gitlab.name}',
                'policy_arn': 'arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore'
            },
            'gitlab_ssm_cloudwatch': {
                'role': '${aws_iam_role.gitlab.name}',
                'policy_arn': 'arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy'
            },
            # Since we are using the boundary as a policy Gitlab can explicitly
            # do everything within the boundary
            'gitlab_boundary': {
                'role': '${aws_iam_role.gitlab.name}',
                'policy_arn': '${aws_iam_policy.gitlab_boundary.arn}'
            },
            'gitlab_vpc': {
                'role': '${aws_iam_role.gitlab_vpc.name}',
                'policy_arn': '${aws_iam_policy.gitlab_vpc.arn}'
            }
        },
        'aws_inspector2_enabler': {
            'gitlab': {
                'account_ids': [aws.account],
                'resource_types': ['ECR', 'EC2'],
                'depends_on': ['aws_iam_service_linked_role.gitlab_ssm']
            }
        },
        'google_service_account': {
            'gitlab': {
                'project': '${local.google_project}',
                'account_id': name,
                'display_name': name,
            }
            for name in ['azul-gitlab']
        },
        'google_project_iam_member': {
            'gitlab_' + name: {
                'project': '${local.google_project}',
                'role': role,
                'member': 'serviceAccount:${google_service_account.gitlab.email}'
            }
            for name, role in [
                ('write', '${google_project_iam_custom_role.gitlab.id}'),
                ('read', 'roles/viewer')
            ]
        },
        'google_project_iam_custom_role': {
            'gitlab': {
                'role_id': 'azul_gitlab',
                'title': 'azul_gitlab',
                'permissions': [
                    'resourcemanager.projects.setIamPolicy',
                    *(
                        f'iam.{resource}.{operation}'
                        for operation in ['create', 'delete', 'get', 'list', 'update', 'undelete']
                        for resource in ['roles', 'serviceAccountKeys', 'serviceAccounts']
                        if resource != 'serviceAccountKeys' or operation not in ['update', 'undelete']
                    ),
                    'serviceusage.services.use'
                ]
            }
        },
        'aws_instance': {
            'gitlab': {
                'iam_instance_profile': '${aws_iam_instance_profile.gitlab.name}',
                'ami': ami_id[config.region],
                'instance_type': 't3a.xlarge',
                'metadata_options': {
                    'http_endpoint': 'enabled',
                    'http_tokens': 'required',
                    # This value was empirically determined. With a lower value
                    # builds in GitLab failed with NoCredentialsError.
                    'http_put_response_hop_limit': 3
                },
                'root_block_device': {
                    'encrypted': True,
                    'volume_size': 20
                },
                'key_name': '${aws_key_pair.gitlab.key_name}',
                'network_interface': {
                    'network_interface_id': '${aws_network_interface.gitlab.id}',
                    'device_index': 0
                },
                'user_data_replace_on_change': True,
                'user_data': '#cloud-config\n' + yaml.dump({
                    'mounts': [
                        ['/dev/nvme1n1', gitlab_mount, 'ext4', '']
                    ],
                    'packages': [
                        'docker',
                        'amazon-cloudwatch-agent',
                        'amazon-ecr-credential-helper',
                        'dracut-fips'
                    ],
                    'ssh_authorized_keys': other_public_keys.get(config.deployment_stage, []),
                    'bootcmd': [
                        [
                            'cloud-init-per',
                            'once',
                            'disable-ssm',
                            'systemctl',
                            'disable',
                            '--now',
                            'amazon-ssm-agent.service'
                        ]
                    ],
                    'write_files': [
                        {
                            'path': '/root/.docker/config.json',
                            'permissions': '0644',
                            'owner': 'root',
                            'content': json.dumps(
                                {
                                    'credHelpers': {
                                        config.docker_registry[:-1]: 'ecr-login'
                                    }
                                }
                                if config.docker_registry else
                                {}
                            )
                        },
                        {
                            'path': '/etc/systemd/system/gitlab-dind.service',
                            'permissions': '0644',
                            'owner': 'root',
                            'content': jl(
                                '[Unit]',
                                'Description=Docker-in-Docker service for GitLab',
                                'After=docker.service',
                                'Requires=docker.service',
                                '[Service]',
                                # We explicitly configure Docker (see /etc/docker/daemon.json) to log to
                                # journald, so we don't need systemd to capture process output.
                                'StandardOutput=null',
                                'StandardError=null',
                                'TimeoutStartSec=5min',  # `docker pull` may take a long time
                                'Restart=always',
                                'ExecStartPre=-/usr/bin/docker stop gitlab-dind',
                                'ExecStartPre=-/usr/bin/docker rm gitlab-dind',
                                'ExecStartPre=-/usr/bin/docker network rm gitlab-runner-net',
                                'ExecStartPre=/usr/bin/docker network create gitlab-runner-net',
                                'ExecStartPre=/usr/bin/docker pull ' + dind_image,
                                jw(
                                    'ExecStart=/usr/bin/docker',
                                    'run',
                                    '--name gitlab-dind',
                                    '--privileged',
                                    '--rm',
                                    '--network gitlab-runner-net',
                                    # The following option makes dockerd
                                    # listen on port 2375 without TLS. By
                                    # default, dockerd only listens on 2376
                                    # with TLS. The port is not exposed and
                                    # can only be reached from other
                                    # containers on the dedicated
                                    # gitlab-runner-net network, so TLS is
                                    # unnecessary.
                                    '--env DOCKER_TLS_CERTDIR=',
                                    # This DinD container is attached to a
                                    # custom network. Because of that, Docker
                                    # provides an /etc/resolv.conf that
                                    # configures the container to use the DNS
                                    # server embedded in the Docker daemon,
                                    # which is reachable in the container at
                                    # 127.0.0.11. This is a localhost-like alias
                                    # that would be ignored in second-tier
                                    # containers started by the Docker-daemon
                                    # running in the DinD container, causing
                                    # that container, or rather the embedded DNS
                                    # server it's configured to use, to fall
                                    # back to the reckless defaults hard-coded
                                    # in the Docker source: 8.8.8.8 and 8.8.4.4.
                                    # These servers are operated by Google and
                                    # are rate-limited by source IP. All the
                                    # VPC's egress traffic is routed through a
                                    # NAT so all requests to these servers made
                                    # within the VPC would appear to originate
                                    # from the same public IP, therefore sharing
                                    # one rate limit, causing them to be dropped
                                    # whenever the rate limit is tripped.
                                    #
                                    # By mounting the host's resolv.conf into
                                    # the Dind container, we work around this
                                    # issue, so that containers launched by the
                                    # Docker daemon running in the Dind
                                    # container have a functional non-localhost
                                    # DNS server and don't fall back to the
                                    # Google ones.
                                    '--volume /etc/resolv.conf:/etc/resolv.conf',
                                    f'--volume {gitlab_mount}/docker:/var/lib/docker',
                                    f'--volume {gitlab_mount}/runner/config:/etc/gitlab-runner',
                                    dind_image
                                ),
                                '[Install]',
                                'WantedBy=multi-user.target',
                            )
                        },
                        {
                            'path': '/etc/systemd/system/gitlab.service',
                            'permissions': '0644',
                            'owner': 'root',
                            'content': jl(
                                '[Unit]',
                                'Description=GitLab service',
                                'After=docker.service',
                                'Requires=docker.service',
                                '[Service]',
                                # We explicitly configure Docker (see /etc/docker/daemon.json) to log to
                                # journald, so we don't need systemd to capture process output.
                                'StandardOutput=null',
                                'StandardError=null',
                                'TimeoutStartSec=5min',  # `docker pull` may take a long time
                                'Restart=always',
                                'ExecStartPre=-/usr/bin/docker stop gitlab',
                                'ExecStartPre=-/usr/bin/docker rm gitlab',
                                'ExecStartPre=/usr/bin/docker pull ' + gitlab_image,
                                jw(
                                    'ExecStart=/usr/bin/docker',
                                    'run',
                                    '--name gitlab',
                                    '--env GITLAB_SKIP_TAIL_LOGS=true',
                                    '--hostname ${aws_route53_record.gitlab.name}',
                                    '--publish 80:80',
                                    '--publish 2222:22',
                                    '--rm',
                                    f'--volume {gitlab_mount}/config:/etc/gitlab',
                                    f'--volume {gitlab_mount}/logs:/var/log/gitlab',
                                    f'--volume {gitlab_mount}/data:/var/opt/gitlab',
                                    gitlab_image
                                ),
                                '[Install]',
                                'WantedBy=multi-user.target'
                            )
                        },
                        {
                            'path': '/etc/systemd/system/gitlab-runner.service',
                            'permissions': '0644',
                            'owner': 'root',
                            'content': jl(
                                '[Unit]',
                                'Description=GitLab runner service',
                                'After=docker.service gitlab-dind.service gitlab.service',
                                'Requires=docker.service gitlab-dind.service gitlab.service',
                                '[Service]',
                                # We explicitly configure Docker (see /etc/docker/daemon.json) to log to
                                # journald, so we don't need systemd to capture process output.
                                'StandardOutput=null',
                                'StandardError=null',
                                'TimeoutStartSec=5min',  # `docker pull` may take a long time
                                'Restart=always',
                                'ExecStartPre=-/usr/bin/docker stop gitlab-runner',
                                'ExecStartPre=-/usr/bin/docker rm gitlab-runner',
                                'ExecStartPre=/usr/bin/docker pull ' + runner_image,
                                jw(
                                    'ExecStart=/usr/bin/docker',
                                    'run',
                                    '--name gitlab-runner',
                                    '--rm',
                                    f'--volume {gitlab_mount}/runner/config:/etc/gitlab-runner',
                                    '--network gitlab-runner-net',
                                    '--env DOCKER_HOST=tcp://gitlab-dind:2375',
                                    runner_image
                                ),
                                '[Install]',
                                'WantedBy=multi-user.target'
                            )
                        },
                        {
                            'path': '/etc/systemd/system/clamscan.service',
                            'permissions': '0644',
                            'owner': 'root',
                            'content': jl(
                                '[Unit]',
                                'Description=ClamAV malware scan of entire file system',
                                'After=docker.service gitlab-dind.service',
                                'Requires=docker.service gitlab-dind.service',
                                '[Service]',
                                # We explicitly configure Docker (see /etc/docker/daemon.json) to log to
                                # journald, so we don't need systemd to capture process output.
                                'StandardOutput=null',
                                'StandardError=null',
                                'Type=simple',
                                'TimeoutStartSec=5min',  # `docker pull` may take a long time
                                'ExecStartPre=-/usr/bin/docker stop clamscan',
                                'ExecStartPre=-/usr/bin/docker rm clamscan',
                                'ExecStartPre=/usr/bin/docker pull ' + clamav_image,
                                jw(
                                    'ExecStart=/usr/bin/docker',
                                    'run',
                                    '--name clamscan',
                                    '--rm',
                                    '--volume /var/run/docker.sock:/var/run/docker.sock',
                                    '--volume /:/scan:ro',
                                    f'--volume {gitlab_mount}/clamav:/var/lib/clamav:rw',
                                    clamav_image,
                                    '/bin/sh',
                                    '-c',
                                    qq(
                                        'freshclam',
                                        '&& echo freshclam succeeded',
                                        '|| (echo freshclam failed; false)',
                                        '&& clamscan',
                                        '--recursive',
                                        '--infected',  # Only print infected files
                                        '--allmatch=yes',  # Continue scanning within file after a match
                                        '--exclude-dir=^/scan/var/lib/docker/overlay2/.*/merged/sys',
                                        '--exclude-dir=^/scan/var/lib/docker/overlay2/.*/merged/proc',
                                        '--exclude-dir=^/scan/var/lib/docker/overlay2/.*/merged/dev',
                                        '--exclude-dir=^/scan/sys',
                                        '--exclude-dir=^/scan/proc',
                                        '--exclude-dir=^/scan/dev',
                                        '/scan',
                                        '&& echo clamscan succeeded',
                                        '|| (echo clamscan failed; false)'
                                    )
                                ),
                                '[Install]',
                                'WantedBy='
                            )
                        },
                        {
                            'path': '/etc/systemd/system/clamscan.timer',
                            'permissions': '0644',
                            'owner': 'root',
                            'content': jl(
                                '[Unit]',
                                'Description=Scheduled ClamAV malware scan of entire file system',
                                '[Timer]',
                                'OnCalendar=Sun *-*-* 8:0:0',
                                '[Install]',
                                'WantedBy=timers.target'
                            )
                        },
                        {
                            'path': '/etc/systemd/system/prune-images.service',
                            'permissions': '0644',
                            'owner': 'root',
                            'content': jl(
                                '[Unit]',
                                'Description=Pruning of stale docker images',
                                'After=docker.service gitlab-dind.service',
                                'Requires=docker.service gitlab-dind.service',
                                '[Service]',
                                # We explicitly configure Docker (see /etc/docker/daemon.json) to log to
                                # journald, so we don't need systemd to capture process output.
                                'StandardOutput=null',
                                'StandardError=null',
                                'Type=simple',
                                'TimeoutStartSec=5min',  # `docker pull` may take a long time
                                'ExecStartPre=-/usr/bin/docker stop prune-images',
                                'ExecStartPre=-/usr/bin/docker rm prune-images',
                                'ExecStartPre=/usr/bin/docker pull ' + dind_image,
                                jw(
                                    'ExecStart=/usr/bin/docker',
                                    'exec',  # Execute (as in `docker exec`) …
                                    'gitlab-dind',  # … inside the gitlab-dind container …
                                    'docker',  # … the docker …
                                    'image',  # … image command …
                                    'prune',  # … to delete, …
                                    '--force',  # … without prompting for confirmation, …
                                    '--all',  # … all images …
                                    f'--filter "until={90 * 24}h"',  # … except those from more recent builds.
                                    #
                                    # If we deleted more recent images, we
                                    # would risk failing the requirements
                                    # check on sandbox builds since that
                                    # check depends on image caching. The
                                    # deadline below assumes that the most
                                    # recent pipeline was run less than a
                                    # month ago.
                                ),
                                '[Install]',
                                'WantedBy='
                            )
                        },
                        {
                            'path': '/etc/systemd/system/prune-images.timer',
                            'permissions': '0644',
                            'owner': 'root',
                            'content': jl(
                                '[Unit]',
                                'Description=Scheduled pruning of stale docker images',
                                '[Timer]',
                                'OnCalendar=Sat *-*-* 12:0:0',
                                '[Install]',
                                'WantedBy=timers.target'
                            )
                        },
                        {
                            'path': '/etc/systemd/system/registry-garbage-collect.service',
                            'permissions': '0644',
                            'owner': 'root',
                            'content': jl(
                                '[Unit]',
                                'Description=GitLab container registry garbage collection',
                                'After=docker.service gitlab.service',
                                'Requires=docker.service gitlab.service',
                                '[Service]',
                                # We explicitly configure Docker (see /etc/docker/daemon.json) to log to
                                # journald, so we don't need systemd to capture process output.
                                'StandardOutput=null',
                                'StandardError=null',
                                'Type=simple',
                                'TimeoutStartSec=5min',  # `docker pull` may take a long time
                                'ExecStartPre=-/usr/bin/docker stop registry-garbage-collect',
                                'ExecStartPre=-/usr/bin/docker rm registry-garbage-collect',
                                'ExecStartPre=/usr/bin/docker pull ' + gitlab_image,
                                jw(
                                    'ExecStart=/usr/bin/docker',
                                    'exec',  # Execute (as in `docker exec`) …
                                    'gitlab',  # … inside the gitlab container …
                                    '/opt/gitlab/bin/gitlab-ctl',  # … the gitlab-ctl …
                                    'registry-garbage-collect',  # … garbage collect command
                                    '-m'  # … and delete untagged images
                                ),
                                '[Install]',
                                'WantedBy='
                            )
                        },
                        {
                            'path': '/etc/systemd/system/registry-garbage-collect.timer',
                            'permissions': '0644',
                            'owner': 'root',
                            'content': jl(
                                '[Unit]',
                                'Description=Scheduled GitLab container registry garbage collection',
                                '[Timer]',
                                'OnCalendar=Sat *-*-* 14:0:0',
                                '[Install]',
                                'WantedBy=timers.target'
                            )
                        },
                        {
                            # AWS recommends placing the amazon-cloudwatch-agent config file at this path.
                            # Note that the parent of etc/ is where the agent is installed.
                            'path': '/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json',
                            'permissions': '0664',
                            'owner': 'root',
                            'content': json.dumps({
                                'agent': {
                                    'region': aws.region_name,
                                    'logfile': '/opt/aws/amazon-cloudwatch-agent/logs/amazon-cloudwatch-agent.log',
                                    'debug': bool(config.debug)
                                },
                                'metrics': {
                                    'metrics_collected': {
                                        'cpu': {
                                            'append_dimensions': {
                                                'InstanceName': 'azul-gitlab'
                                            },
                                            'resources': [
                                                "*"
                                            ],
                                            'measurement': [
                                                'cpu_usage_active'
                                            ]
                                        },
                                        'mem': {
                                            'append_dimensions': {
                                                'InstanceName': 'azul-gitlab'
                                            },
                                            'measurement': [
                                                'used_percent'
                                            ],
                                        },
                                        'disk': {
                                            'append_dimensions': {
                                                'InstanceName': 'azul-gitlab'
                                            },
                                            'measurement': [
                                                'used_percent'
                                            ],
                                            'resources': [
                                                '/',
                                                gitlab_mount
                                            ],
                                            # This drops the name of the device
                                            # from the dimensions in each data
                                            # point. Since the device name
                                            # correlates with the mount point,
                                            # maintaining that dimension is
                                            # redundant. Note that we cannot
                                            # drop the fstype dimension this
                                            # way, and therefore have to
                                            # specify it explicitly when we
                                            # create the alarm below.
                                            'drop_device': True
                                        }
                                    },
                                    # The dimensions appended to the specific
                                    # metrics are only appended if we specify
                                    # a global append_dimensions field as well.
                                    'append_dimensions': {
                                    },
                                },
                                'logs': {
                                    'logs_collected': {
                                        'files': {
                                            'collect_list': [
                                                {
                                                    'file_path': path,
                                                    'log_group_name': '${aws_cloudwatch_log_group.gitlab_cwagent.name}',
                                                    # https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_CreateLogStream.html
                                                    # Characters disallowed for use in a log stream name are `:` and
                                                    # `*`, so we replace any occurrence of `*` in `path` with `?`.
                                                    'log_stream_name': path.replace('*', '?')
                                                }
                                                for path in
                                                [
                                                    f'/var/log/{file}'
                                                    for file in
                                                    [
                                                        'amazon/ssm/amazon-ssm-agent.log',
                                                        'audit/audit.log',
                                                        'cloud-init.log',
                                                        'cron',
                                                        'maillog',
                                                        'messages',
                                                        'secure'
                                                    ]

                                                ] + [
                                                    f'{gitlab_mount}/logs/{file}.log'
                                                    for file in
                                                    [
                                                        'gitaly/gitaly_ruby_json',
                                                        'gitlab-shell/gitlab-shell',
                                                        'nginx/gitlab_access',
                                                        'nginx/gitlab_error',
                                                        'nginx/gitlab_registry_access',
                                                        'puma/puma_stderr',
                                                        'puma/puma_stdout',
                                                        # The '*' is used in order to get the most recent GitLab
                                                        # reconfigure logs (name based on UNIX timestamp of when
                                                        # reconfigure initiated). Only the most recent file, by
                                                        # modification time, matching the wildcard is collected.
                                                        'reconfigure/*'
                                                    ]
                                                ] + [
                                                    f'{gitlab_mount}/logs/gitlab-rails/{file}.log'
                                                    for file in
                                                    [
                                                        'api_json',
                                                        'application_json',
                                                        'application',
                                                        'audit_json',
                                                        'auth',
                                                        'database_load_balancing',
                                                        'exceptions_json',
                                                        'graphql_json',
                                                        'migrations',
                                                        'production_json',
                                                        'production',
                                                        'sidekiq_client'
                                                    ]
                                                ]
                                            ]
                                        }
                                    }
                                }
                                # FIXME: Re-enable formatting of the JSON above
                                #        https://github.com/DataBiosphere/azul/issues/5314
                            })
                        },
                        {
                            'path': '/etc/docker/daemon.json',
                            'permissions': '0644',
                            'owner': 'root',
                            'content': json.dumps(
                                {
                                    'log-driver': 'journald',
                                    'log-opts': {
                                        'tag': 'docker: {{.Name}}'
                                    }
                                }
                            )
                        },
                    ],
                    'runcmd': [
                        ['systemctl', 'daemon-reload'],
                        ['dracut', '-f'],
                        ['/sbin/grubby', '--update-kernel=ALL', '--args="fips=1"'],
                        [
                            # Key exchange algorithm curve25519 is not FIPS-compliant
                            'sed',
                            '--in-place',
                            's/curve25519[^,]*,//g',
                            '/etc/ssh/sshd_config'
                        ],
                        [
                            'systemctl',
                            'enable',
                            '--now',  # also start the units
                            '--no-block',  # avoid deadlock with cloud-init which is an active systemd unit, too
                            'docker',
                            'gitlab-dind',
                            'gitlab',
                            'gitlab-runner',
                            'clamscan.timer',
                            'prune-images.timer',
                            'registry-garbage-collect.timer'
                        ],
                        [
                            'amazon-cloudwatch-agent-ctl',
                            '-m', 'ec2',  # mode
                            '-a', 'fetch-config',  # action (fetch file from location specified in -c)
                            '-c', 'file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json',
                            '-s'  # restart agent afterwards
                        ],
                        ['yum', '-y', 'update'],
                        ['systemctl', 'enable', '--now', 'amazon-ssm-agent.service']
                    ],
                    # Reboot to realize the added kernel parameter the changed sshd configuration
                    'power_state': {
                        'mode': 'reboot'
                    },
                }, indent=2),
                'tags': {
                    'Owner': config.owner
                }
            }
        },
        'aws_cloudwatch_metric_alarm': {
            **{
                'gitlab_' + resource: {
                    'alarm_name': 'azul-gitlab_' + resource,
                    'comparison_operator': 'GreaterThanOrEqualToThreshold',
                    'datapoints_to_alarm': 6,
                    'evaluation_periods': 6,
                    'period': 60 * 10,
                    'metric_name': metric,
                    'namespace': 'CWAgent',
                    'dimensions': dimensions | {
                        # Instead of using 'InstanceId' here, we use a custom
                        # dimension that has been appended to each metric. This
                        # removes the need to recreate the alarm every time the
                        # EC2 instance is recreated, which avoids the alarm from
                        # entering an insufficient_data state every time the new
                        # EC2 instance is launched for a first time.
                        'InstanceName': 'azul-gitlab'
                    },
                    'statistic': stat,
                    'threshold': threshold,
                    'treat_missing_data': 'missing',
                    **{
                        state + '_actions': ['${data.aws_sns_topic.monitoring.arn}']
                        for state in ('insufficient_data', 'alarm', 'ok')
                    },
                } for resource, metric, stat, threshold, dimensions in
                [
                    # FIXME: Add `mem_used_percent` alarm
                    #        https://github.com/DataBiosphere/azul/issues/5139
                    ('data_disk_use', 'disk_used_percent', 'Maximum', 75, {'path': gitlab_mount, 'fstype': 'ext4'}),
                    ('root_disk_use', 'disk_used_percent', 'Maximum', 75, {'path': '/', 'fstype': 'xfs'}),
                    ('cpu_use', 'cpu_usage_active', 'Average', 90, {'cpu': 'cpu-total'})
                ]
            }
        }
    }
})
