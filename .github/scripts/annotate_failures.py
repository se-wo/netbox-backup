"""
Turn Django test runner failures into GitHub Actions error annotations, so failing tests are visible in the
run summary and on pull requests without opening the raw log.
"""
import re
import sys

SEPARATOR = re.compile(r'^(=|-){70}$')


def failures(lines):
    block = None
    for line in lines:
        header = re.match(r'^(ERROR|FAIL): (\S+) \((.+)\)', line)
        if header:
            if block:
                yield block
            block = [line]
        elif block is not None:
            if SEPARATOR.match(line) and len(block) > 2:
                yield block
                block = None
            elif not SEPARATOR.match(line):
                block.append(line)
    if block:
        yield block


def escape(text):
    return text.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def escape_property(text):
    return escape(text).replace(':', '%3A').replace(',', '%2C')


def main(path):
    with open(path, errors='replace') as f:
        lines = f.read().splitlines()
    found = list(failures(lines))
    for block in found[:10]:
        title = block[0]
        body = '\n'.join(block[1:])[-3500:]
        print(f'::error title={escape_property(title)}::{escape(body)}')
    if not found:
        tail = '\n'.join(lines[-60:])
        print(f'::error title=Test run failed (no test failures parsed)::{escape(tail)}')


if __name__ == '__main__':
    main(sys.argv[1])
