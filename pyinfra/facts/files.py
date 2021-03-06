from __future__ import unicode_literals

import re

from datetime import datetime

from six.moves import shlex_quote

from pyinfra.api.connectors.util import escape_unix_path
from pyinfra.api.facts import FactBase
from pyinfra.api.util import try_int

LINUX_STAT_COMMAND = (
    "stat -c 'user=%U group=%G mode=%A atime=%X mtime=%Y ctime=%Z size=%s %N'"
)
BSD_STAT_COMMAND = (
    "stat -f 'user=%Su group=%Sg mode=%Sp atime=%a mtime=%m ctime=%c size=%z %N%SY'"
)

FLAG_TO_TYPE = {
    'b': 'block',
    'c': 'character',
    'd': 'directory',
    'l': 'link',
    's': 'socket',
    'p': 'fifo',
    '-': 'file',
}

SYMBOL_TO_OCTAL_PERMISSIONS = {
    'rwx': '7',
    'rw-': '6',
    'r-x': '5',
    'r--': '4',
    '-wx': '3',
    '-w-': '2',
    '--x': '1',
}


def _parse_mode(mode):
    '''
    Converts ls mode output (rwxrwxrwx) -> integer (755).
    '''

    result = ''
    # owner, group, world
    for group in [mode[0:3], mode[3:6], mode[6:9]]:
        if group in SYMBOL_TO_OCTAL_PERMISSIONS:
            result = '{0}{1}'.format(result, SYMBOL_TO_OCTAL_PERMISSIONS[group])
        else:
            result = '{0}0'.format(result)

    return int(result)


class File(FactBase):
    # Types must match FLAG_TO_TYPE in .util.files.py
    type = 'file'

    def command(self, path):
        path = escape_unix_path(path)
        return (
            'stat {path} 1> /dev/null 2> /dev/null && '  # check file exists
            '({linux_stat_command} {path} 2> /dev/null || {bsd_stat_command} {path}) '
            '|| true'  # don't error if the file does not exist (return None)
        ).format(
            path=path,
            linux_stat_command=LINUX_STAT_COMMAND,
            bsd_stat_command=BSD_STAT_COMMAND,
        )

    def process(self, output):
        stat_bits = output[0].split(None, 7)
        stat_bits, filename = stat_bits[:-1], stat_bits[-1]

        data = {}
        path_type = None

        for bit in stat_bits:
            key, value = bit.split('=')

            if key == 'mode':
                path_type = FLAG_TO_TYPE[value[0]]
                value = _parse_mode(value[1:])

            elif key == 'size':
                value = try_int(value)

            elif key in ('atime', 'mtime', 'ctime'):
                value = try_int(value)
                if isinstance(value, int):
                    value = datetime.utcfromtimestamp(value)

            data[key] = value

        if path_type != self.type:
            return False

        if path_type == 'link':
            filename, target = filename.split(' -> ')
            data['link_target'] = target.strip("'")

        return data


class Link(File):
    type = 'link'


class Directory(File):
    type = 'directory'


class Socket(File):
    type = 'socket'


class Sha1File(FactBase):
    '''
    Returns a SHA1 hash of a file. Works with both sha1sum and sha1.
    '''

    # If the file doesn't exist, return `None` instead of failing
    use_default_on_error = True

    _regexes = [
        r'^([a-zA-Z0-9]{40})\s+%s$',
        r'^SHA1\s+\(%s\)\s+=\s+([a-zA-Z0-9]{40})$',
    ]

    def command(self, name):
        name = escape_unix_path(name)
        self.name = name
        return 'sha1sum {0} 2> /dev/null || shasum {0} 2> /dev/null || sha1 {0}'.format(name)

    def process(self, output):
        for regex in self._regexes:
            regex = regex % self.name
            matches = re.match(regex, output[0])
            if matches:
                return matches.group(1)


class Sha256File(FactBase):
    '''
    Returns a SHA256 hash of a file.
    '''

    use_default_on_error = True

    _regexes = [
        r'^([a-zA-Z0-9]{64})\s+%s$',
        r'^SHA256\s+\(%s\)\s+=\s+([a-zA-Z0-9]{64})$',
    ]

    def command(self, name):
        name = escape_unix_path(name)
        self.name = name
        return (
            'sha256sum {0} 2> /dev/null '
            '|| shasum -a 256 {0} 2> /dev/null '
            '|| sha256 {0}'
        ).format(name)

    def process(self, output):
        for regex in self._regexes:
            regex = regex % self.name
            matches = re.match(regex, output[0])
            if matches:
                return matches.group(1)


class Md5File(FactBase):
    '''
    Returns an MD5 hash of a file.
    '''

    use_default_on_error = True

    _regexes = [
        r'^([a-zA-Z0-9]{32})\s+%s$',
        r'^SHA256\s+\(%s\)\s+=\s+([a-zA-Z0-9]{32})$',
    ]

    def command(self, name):
        name = escape_unix_path(name)
        self.name = name
        return 'md5sum {0} 2> /dev/null || md5 {0}'.format(name)

    def process(self, output):
        for regex in self._regexes:
            regex = regex % self.name
            matches = re.match(regex, output[0])
            if matches:
                return matches.group(1)


class FindInFile(FactBase):
    '''
    Checks for the existence of text in a file using grep. Returns a list of matching
    lines if the file exists, and ``None`` if the file does not.
    '''

    use_default_on_error = True

    def command(self, name, pattern):
        name = escape_unix_path(name)
        pattern = shlex_quote(pattern)

        self.name = name

        return (
            'grep -e {0} {1} 2> /dev/null || '
            '(find {1} -type f > /dev/null && echo "__pyinfra_exists_{1}")'
        ).format(pattern, name).strip()

    def process(self, output):
        # If output is the special string: no matches, so return an empty list;
        # this allows us to differentiate between no matches in an existing file
        # or a file not existing.
        if output and output[0] == '__pyinfra_exists_{0}'.format(self.name):
            return []

        return output


class FindFiles(FactBase):
    '''
    Returns a list of files from a start point, recursively using find.
    '''

    @staticmethod
    def command(name):
        return 'find {0} -type f'.format(escape_unix_path(name))

    @staticmethod
    def process(output):
        return output


class FindLinks(FindFiles):
    '''
    Returns a list of links from a start point, recursively using find.
    '''

    @staticmethod
    def command(name):
        return 'find {0} -type l'.format(escape_unix_path(name))


class FindDirectories(FindFiles):
    '''
    Returns a list of directories from a start point, recursively using find.
    '''

    @staticmethod
    def command(name):
        return 'find {0} -type d'.format(escape_unix_path(name))
