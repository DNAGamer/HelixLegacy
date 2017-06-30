import sys
import inspect
import logging
import os
from time import gmtime, strftime

from textwrap import dedent
from discord.ext.commands.bot import _get_variable

from .core import HelpfulError

class Yikes:
    def find_module(self, fullname, path=None):
        if fullname == 'requests':
            return self
        return None

    def _get_import_chain(self, *, until=None):
        stack = inspect.stack()[2:]
        try:
            for frameinfo in stack:
                try:
                    if not frameinfo.code_context:
                        continue

                    data = dedent(''.join(frameinfo.code_context))
                    if data.strip() == until:
                        raise StopIteration

                    yield frameinfo.filename, frameinfo.lineno, data.strip()
                    del data
                finally:
                    del frameinfo
        finally:
            del stack

    def _format_import_chain(self, chain, *, message=None):
        lines = []
        for line in chain:
            lines.append("In %s, line %s:\n    %s" % line)

        if message:
            lines.append(message)

        return '\n'.join(lines)

    def load_module(self, name):
        if _get_variable('allow_requests'):
            sys.meta_path.pop(0)
            return __import__('requests')

        import_chain = tuple(self._get_import_chain(until='from .bot import Helix'))
        import_tb = self._format_import_chain(import_chain)

sys.meta_path.insert(0, Yikes())

from .bot import Helix
from .core import BetterLogRecord

__all__ = ['MusicBot']

logging.setLogRecordFactory(BetterLogRecord)

_func_prototype = "def {logger_func_name}(self, message, *args, **kwargs):\n" \
                  "    if self.isEnabledFor({levelname}):\n" \
                  "        self._log({levelname}, message, args, **kwargs)"

def _add_logger_level(levelname, level, *, func_name = None):
    """

    :type levelname: str
        The reference name of the level, e.g. DEBUG, WARNING, etc
    :type level: int
        Numeric logging level
    :type func_name: str
        The name of the logger function to log to a level, e.g. "info" for log.info(...)
    """

    func_name = func_name or levelname.lower()

    setattr(logging, levelname, level)
    logging.addLevelName(level, levelname)

    exec(_func_prototype.format(logger_func_name=func_name, levelname=levelname), logging.__dict__, locals())
    setattr(logging.Logger, func_name, eval(func_name))


_add_logger_level('EVERYTHING', 1)
_add_logger_level('NOISY', 4, func_name='noise')
_add_logger_level('FFMPEG', 5)
_add_logger_level('VOICEDEBUG', 6)

log = logging.getLogger(__name__)
log.setLevel(logging.EVERYTHING)

if not os.path.exists("logs"):
    log.info("logging folder doesnt exist")
    os.mkdir("logs")
if os.path.isfile("logs/bot.log"):
    log.info("Moving old bot log")
    try:
        if not os.path.exists("logs/archive"):
            log.info("archive folder doesnt exist. creating")
            os.mkdir("logs/archive")
        if os.path.exists("logs/archive"):
            name = "logs/archive/" + (str(strftime("%Y-%m-%d  %H-%M-%S", gmtime()))) + ".log"
            if os.path.isfile(name):
                time.sleep(2)
                name = "log/archive/" + (str(strftime("%Y-%m-%d  %H:%M:%S", gmtime()))) + ".log"
            os.rename("logs/bot.log", name)
    except Exception as e:
        log.critical("unable to create logging file")
        log.critical(e)

fhandler = logging.FileHandler(filename='logs/bot.log', encoding='utf-8', mode='a')
fhandler.setFormatter(logging.Formatter(
    "[%(levelname)s] %(name)s: %(message)s"))
log.addHandler(fhandler)

del _func_prototype
del _add_logger_level
del fhandler
