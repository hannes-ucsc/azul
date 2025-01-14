from datetime import (
    datetime,
)
import json
import logging
from operator import (
    itemgetter,
)
import subprocess
import zoneinfo

tz = zoneinfo.ZoneInfo('America/Los_Angeles')

now = datetime.now(tz)

log = logging.getLogger('azul.github.schedule')


def create_upgrade_issue():
    # Pick any date for `start` and an issue will be created on that date, as
    # well as every two weeks before and after, but only in the future. Note
    # that this doesn't mean that the start date has to lie in the future.
    start = datetime(2023, 11, 27, tzinfo=tz)  # Monday, …
    if 0 == (now - start).days % 14 and now.hour == 9:  # … every other week, at 9am
        template = '.github/ISSUE_TEMPLATE/upgrade.md'
        front_matter, body = _load_issue_template(template)
        labels, title = front_matter['labels'], front_matter['title']
        process = subprocess.run([
            'gh', 'issue', 'list',
            f'--search=in:title "{title} {now.date()}"',
            '--json=number',
            '--limit=10',
        ], check=True, stdout=subprocess.PIPE)
        results = json.loads(process.stdout)
        issues = set(map(itemgetter('number'), results))
        if issues:
            log.info('At least one matching issue already exists: %r', issues)
        else:
            subprocess.run([
                'gh', 'issue', 'create',
                f'--title={title} {now.date()}',
                f'--label={labels}',
                f'--body={body}'
            ], check=True)
    else:
        log.info('Current time is outside of the configured schedule window')


def _load_issue_template(path: str) -> tuple[dict[str, str], str]:
    """
    Load the issue template at the given path and parse any YAML front-matter
    embedded in the template. GitHub uses the front-matter in issue templates to
    allow for customizing issue properties other than the issue's description,
    such as its title.

    https://jekyllrb.com/docs/front-matter/

    :return: A tuple of front matter, a dictionary, and the body, a str. If
             there is no front-matter or if it is empty, the first element will
             be an empty dictionary
    """
    with open(path) as f:
        front_matter = {}
        line = f.readline()
        sep = '---\n'
        if line == sep:
            for line in f:
                if line == sep:
                    break
                else:
                    k, _, v = line.partition(':')
                    front_matter[k.strip()] = v.strip()
        else:
            f.seek(0)
        return front_matter, f.read()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)+7s %(name)s: %(message)s')
    create_upgrade_issue()
