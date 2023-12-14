"""
Fetch AWS Inspector findings and generate a CSV file with the results grouped by
vulnerability.
"""
from collections import (
    defaultdict,
)
import csv
import datetime
import json
import logging
import sys

from azul.deployment import (
    aws,
)
from azul.logging import (
    configure_script_logging,
)
from azul.types import (
    JSON,
    JSONs,
)

log = logging.getLogger(__name__)

SummaryType = dict[str, str | set[str]]


class ParseInspectorFindings:
    all_severities = [
        'CRITICAL',
        'HIGH',
        'MEDIUM',
        'LOW',
        'INFORMATIONAL',
        'UNTRIAGED'
    ]
    default_severities = [
        'CRITICAL',
        'HIGH'
    ]

    @classmethod
    def _parse_args(cls, argv):
        import argparse
        parser = argparse.ArgumentParser(description=__doc__,
                                         formatter_class=argparse.RawTextHelpFormatter)
        parser.add_argument('--severity', '-s',
                            default=','.join(cls.default_severities),
                            help='Limit the results to the severities specified, '
                                 'comma-separated with no spaces.\n'
                                 f"Default: '{','.join(cls.default_severities)}'\n"
                                 f"Choices: {', '.join(cls.all_severities)}")
        parser.add_argument('--json', '-j',
                            default=False, action='store_true',
                            help='Dump all findings to a JSON file.')
        args = parser.parse_args(argv)
        return args

    def __init__(self, argv: list[str]) -> None:
        super().__init__()
        self.args = self._parse_args(argv)
        self.date = datetime.datetime.now().strftime('%Y-%m-%d')
        self.severities = self.args.severity.split(',')
        self.validate_severities()
        self.images = set()
        self.instances = set()

    def validate_severities(self):
        for severity in self.severities:
            if severity not in self.all_severities:
                raise ValueError('Invalid severity', severity)

    def main(self):
        log.info('Fetching all findings from AWS Inspector')
        client = aws.client('inspector2')
        paginator = client.get_paginator('list_findings')
        all_findings = [finding
                        for page in paginator.paginate()
                        for finding in page['findings']]
        if self.args.json:
            self.dump_to_json(all_findings)
        log.info('Fetched %i findings from AWS Inspector with any severity',
                 len(all_findings))
        findings = defaultdict(list)
        for finding in all_findings:
            if finding['severity'] in self.severities:
                vulnerability, summary = self.parse_finding(finding)
                findings[vulnerability].append(summary)
        log.info('Found %i unique vulnerabilities with severity matching %s',
                 len(findings), self.severities)
        self.write_to_csv(findings)
        log.info('Done.')

    def dump_to_json(self, findings: JSONs) -> None:
        output_file_name = f'inspector-findings_{self.date}.json'
        log.info(f'Writing file {output_file_name!r}')
        with open(output_file_name, 'w') as f:
            json.dump({'findings': findings}, f, default=str, indent=4)

    def parse_finding(self, finding: JSON) -> tuple[str, SummaryType]:
        severity = finding['severity']
        # The vulnerabilityId is usually a substring of the finding title (e.g.
        # "CVE-2023-44487" vs"CVE-2023-44487 - google.golang.org/grpc,
        # google.golang.org/grpc"), however this is not always the case,
        # specifically wih the "SNYK-" prefixed vulnerabilityIds, so instead of
        # using the vulnerabilityId we just use the first part of the title.
        vulnerability, _, _ = finding['title'].partition(' ')
        assert len(finding['resources']) == 1, finding
        resource = finding['resources'][0]
        resource_type = resource['type']
        summary = {
            'severity': severity,
            'resource_type': resource_type,
            'resources': set(),
        }
        if resource_type == 'AWS_ECR_CONTAINER_IMAGE':
            for tag in resource['details']['awsEcrContainerImage']['imageTags']:
                repo = resource['details']['awsEcrContainerImage']['repositoryName']
                image = f'{repo}/{tag}'
                summary['resources'].add(image)
                self.images.add(image)
        elif resource_type == 'AWS_EC2_INSTANCE':
            instance_name = resource['details']['awsEc2Instance']['keyName']
            instance_id = resource['id']
            instance = f'{instance_name} {instance_id}'
            summary['resources'].add(instance)
            self.instances.add(instance)
        else:
            assert False, resource
        return vulnerability, summary

    def write_to_csv(self, findings: dict[str, list[SummaryType]]):

        titles = ['Vulnerability', *sorted(self.images), *sorted(self.instances)]
        # A mapping of column titles to column index (0-based)
        lookup = dict(zip(titles, range(len(titles))))

        file_data = [titles]
        for vulnerability, summaries in sorted(findings.items(), reverse=True):
            # A mapping of column index to abbreviated severity value
            column_values = {
                lookup[key]: summary['severity'][0:1]
                for summary in summaries
                for key in summary['resources']
            }
            row = [vulnerability]
            for column_index in range(1, len(titles) + 1):
                row.append(column_values.get(column_index, ''))
            file_data.append(row)

        output_file_name = f'inspector-findings_{self.date}.csv'
        log.info('Writing file: %s', output_file_name)
        with open(output_file_name, mode='w') as csv_file:
            csv_writer = csv.writer(csv_file)
            csv_writer.writerows(file_data)


if __name__ == '__main__':
    configure_script_logging(log)
    parser = ParseInspectorFindings(sys.argv[1:])
    sys.exit(parser.main())