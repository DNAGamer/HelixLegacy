import os.path
import json
import pydoc
import inspect
import logging
import shutil
import textwrap
import discord
import asyncio
import traceback
import configparser
import decimal
import aiohttp
from hashlib import md5
import codecs
import sys
from enum import Enum
from discord.ext.commands.bot import _get_variable
from discord import opus

MAIN_VERSION = "2.0.1"
SUB_VERSION = '-pre_alpha'
BOTVERSION = MAIN_VERSION + SUB_VERSION

AUDIO_CACHE_PATH = os.path.join(os.getcwd(), 'audio_cache')
DISCORD_MSG_CHAR_LIMIT = 2000

log = logging.getLogger(__name__)


class BetterLogRecord(logging.LogRecord):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.relativeCreated /= 1000


class SkipState:
    __slots__ = ['skippers', 'skip_msgs']

    def __init__(self):
        self.skippers = set()
        self.skip_msgs = set()

    @property
    def skip_count(self):
        return len(self.skippers)

    def reset(self):
        self.skippers.clear()
        self.skip_msgs.clear()

    def add_skipper(self, skipper, msg):
        self.skippers.add(skipper)
        self.skip_msgs.add(msg)
        return self.skip_count


class Response:
    __slots__ = ['_content', 'reply', 'delete_after', 'codeblock', '_codeblock']

    def __init__(self, content, reply=False, delete_after=0, codeblock=None):
        self._content = content
        self.reply = reply
        self.delete_after = delete_after
        self.codeblock = codeblock
        self._codeblock = "```{!s}\n{{}}\n```".format('' if codeblock is True else codeblock)

    @property
    def content(self):
        if self.codeblock:
            return self._codeblock.format(self._content)
        else:
            return self._content


# Alright this is going to take some actual thinking through
class AnimatedResponse(Response):
    def __init__(self, content, *sequence, delete_after=0):
        super().__init__(content, delete_after=delete_after)
        self.sequence = sequence


class Serializer(json.JSONEncoder):
    def default(self, o):
        if hasattr(o, '__json__'):
            return o.__json__()

        return super().default(o)

    @classmethod
    def deserialize(cls, data):
        if all(x in data for x in Serializable._class_signature):
            # log.debug("Deserialization requested for %s", data)
            factory = pydoc.locate(data['__module__'] + '.' + data['__class__'])
            # log.debug("Found object %s", factory)
            if factory and issubclass(factory, Serializable):
                # log.debug("Deserializing %s object", factory)
                return factory._deserialize(data['data'], **cls._get_vars(factory._deserialize))

        return data

    @classmethod
    def _get_vars(cls, func):
        # log.debug("Getting vars for %s", func)
        params = inspect.signature(func).parameters.copy()
        args = {}
        # log.debug("Got %s", params)

        for name, param in params.items():
            # log.debug("Checking arg %s, type %s", name, param.kind)
            if param.kind is param.POSITIONAL_OR_KEYWORD and param.default is None:
                # log.debug("Using var %s", name)
                args[name] = _get_variable(name)
                # log.debug("Collected var for arg '%s': %s", name, args[name])

        return args


class Serializable:
    _class_signature = ('__class__', '__module__', 'data')

    def _enclose_json(self, data):
        return {
            '__class__': self.__class__.__qualname__,
            '__module__': self.__module__,
            'data': data
        }

    # Perhaps convert this into some sort of decorator
    @staticmethod
    def _bad(arg):
        raise TypeError('Argument "%s" must not be None' % arg)

    def serialize(self, *, cls=Serializer, **kwargs):
        return json.dumps(self, cls=cls, **kwargs)

    def __json__(self):
        raise NotImplementedError

    @classmethod
    def _deserialize(cls, raw_json, **kwargs):
        raise NotImplementedError


class VoiceStateUpdate:
    class Change(Enum):
        RESUME     = 0   # Reconnect to an existing voice session
        JOIN       = 1   # User has joined the bot's voice channel
        LEAVE      = 2   # User has left the bot's voice channel
        MOVE       = 3   # User has moved voice channels on this server
        CONNECT    = 4   # User has connected to voice chat on this server
        DISCONNECT = 5   # User has disconnected from voice chat on this server
        MUTE       = 6   # User is now mute
        UNMUTE     = 7   # User is no longer mute
        DEAFEN     = 8   # User is now deaf
        UNDEAFEN   = 9   # User is no longer deaf
        AFK        = 10  # User has gone afk
        UNAFK      = 11  # User has come back from afk

        def __repr__(self):
            return self.name

    __slots__ = ['before', 'after', 'broken']

    def __init__(self, before: discord.Member, after: discord.Member):
        self.broken = False
        if not all([before, after]):
            self.broken = True
            return

        self.before = before
        self.after = after

    @property
    def me(self) -> discord.Member:
        return self.after.server.me

    @property
    def is_about_me(self):
        return self.after == self.me

    @property
    def my_voice_channel(self) -> discord.Channel:
        return self.me.voice_channel

    @property
    def is_about_my_voice_channel(self):
        return all((
            self.my_voice_channel,
            self.voice_channel == self.my_voice_channel
        ))

    @property
    def voice_channel(self) -> discord.Channel:
        return self.new_voice_channel or self.old_voice_channel

    @property
    def old_voice_channel(self) -> discord.Channel:
        return self.before.voice_channel

    @property
    def new_voice_channel(self) -> discord.Channel:
        return self.after.voice_channel

    @property
    def server(self) -> discord.Server:
        return self.after.server or self.before.server

    @property
    def member(self) -> discord.Member:
        return self.after or self.before

    @property
    def joining(self):
        return all((
            self.my_voice_channel,
            self.before.voice_channel != self.my_voice_channel,
            self.after.voice_channel == self.my_voice_channel
        ))

    @property
    def leaving(self):
        return all((
            self.my_voice_channel,
            self.before.voice_channel == self.my_voice_channel,
            self.after.voice_channel != self.my_voice_channel
        ))

    @property
    def moving(self):
        return all((
            self.before.voice_channel,
            self.after.voice_channel,
            self.before.voice_channel != self.after.voice_channel,
        ))

    @property
    def connecting(self):
        return all((
            not self.before.voice_channel or self.resuming,
            self.after.voice_channel
        ))

    @property
    def disconnecting(self):
        return all((
            self.before.voice_channel,
            not self.after.voice_channel
        ))

    @property
    def resuming(self):
        return all((
            not self.joining,
            self.is_about_me,
            not self.server.voice_client,
            not self.raw_change
        ))

    def empty(self, *, excluding_me=True, excluding_deaf=False, old_channel=False):
        def check(member):
            if excluding_me and member == self.me:
                return False

            if excluding_deaf and any([member.deaf, member.self_deaf]):
                return False

            return True

        channel = self.old_voice_channel if old_channel else self.voice_channel
        if not channel:
            return

        return not sum(1 for m in channel.voice_members if check(m))

    @property
    def raw_change(self) -> dict:
        return objdiff(self.before.voice, self.after.voice, access_attr='__slots__')

    @property
    def changes(self):
        changes = []
        rchange = self.raw_change

        if 'voice_channel' in rchange:
            if self.joining:
                changes.append(self.Change.JOIN)

            if self.leaving:
                changes.append(self.Change.LEAVE)

            if self.moving:
                changes.append(self.Change.MOVE)

        if self.resuming:
            changes.append(self.Change.RESUME)

        if self.connecting:
            changes.append(self.Change.CONNECT)

        elif self.disconnecting:
            changes.append(self.Change.DISCONNECT)

        if any(s in rchange for s in ['mute', 'self_mute']):
            m = rchange.get('mute', None) or rchange.get('self_mute')
            changes.append(self.Change.MUTE if m[1] else self.Change.UNMUTE)

        if any(s in rchange for s in ['deaf', 'self_deaf']):
            d = rchange.get('deaf', None) or rchange.get('self_deaf')
            changes.append(self.Change.DEAFEN if d[1] else self.Change.UNDEAFEN)

        if 'is_afk' in rchange:
            changes.append(self.Change.MUTE if rchange['is_afk'][1] else self.Change.UNMUTE)

        return changes


# Base class for exceptions
class HelixException(Exception):
    def __init__(self, message, *, expire_in=0):
        super().__init__(message) # ???
        self._message = message
        self.expire_in = expire_in

    @property
    def message(self):
        return self._message

    @property
    def message_no_format(self):
        return self._message


# Something went wrong during the processing of a command
class CommandError(HelixException):
    pass


# Something went wrong during the processing of a song/ytdl stuff
class ExtractionError(HelixException):
    pass


# The no processing entry type failed and an entry was a playlist/vice versa
# TODO: Add typing options instead of is_playlist
class WrongEntryTypeError(ExtractionError):
    def __init__(self, message, is_playlist, use_url):
        super().__init__(message)
        self.is_playlist = is_playlist
        self.use_url = use_url


# FFmpeg complained about something
class FFmpegError(HelixException):
    pass


# FFmpeg complained about something but we don't care
class FFmpegWarning(HelixException):
    pass


# The user doesn't have permission to use a command
class PermissionsError(CommandError):
    @property
    def message(self):
        return "You don't have permission to use that command.\nReason: " + self._message


# Error with pretty formatting for hand-holding users through various errors
class HelpfulError(HelixException):
    def __init__(self, issue, solution, *, preface="An error has occured:", footnote='', expire_in=0):
        self.issue = issue
        self.solution = solution
        self.preface = preface
        self.footnote = footnote
        self.expire_in = expire_in
        self._message_fmt = "\n{preface}\n{problem}\n\n{solution}\n\n{footnote}"

    @property
    def message(self):
        return self._message_fmt.format(
            preface  = self.preface,
            problem  = self._pretty_wrap(self.issue,    "  Problem:"),
            solution = self._pretty_wrap(self.solution, "  Solution:"),
            footnote = self.footnote
        )

    @property
    def message_no_format(self):
        return self._message_fmt.format(
            preface  = self.preface,
            problem  = self._pretty_wrap(self.issue,    "  Problem:", width=None),
            solution = self._pretty_wrap(self.solution, "  Solution:", width=None),
            footnote = self.footnote
        )

    @staticmethod
    def _pretty_wrap(text, pretext, *, width=-1):
        if width is None:
            return '\n'.join((pretext.strip(), text))
        elif width == -1:
            pretext = pretext.rstrip() + '\n'
            width = shutil.get_terminal_size().columns

        lines = textwrap.wrap(text, width=width - 5)
        lines = (('    ' + line).rstrip().ljust(width-1).rstrip() + '\n' for line in lines)

        return pretext + ''.join(lines).rstrip()


class HelpfulWarning(HelpfulError):
    pass


# Base class for control signals
class Signal(Exception):
    pass


# signal to restart the bot
class RestartSignal(Signal):
    pass


# signal to end the bot "gracefully"
class TerminateSignal(Signal):
    pass

class EntryTypes(Enum):
    URL = 1
    STEAM = 2
    FILE = 3

    def __str__(self):
        return self.name


class BasePlaylistEntry(Serializable):
    def __init__(self):
        self.filename = None
        self._is_downloading = False
        self._waiting_futures = []

    @property
    def is_downloaded(self):
        if self._is_downloading:
            return False

        return bool(self.filename)

    async def _download(self):
        raise NotImplementedError

    def get_ready_future(self):
        """
        Returns a future that will fire when the song is ready to be played. The future will either fire with the result (being the entry) or an exception
        as to why the song download failed.
        """
        future = asyncio.Future()
        if self.is_downloaded:
            # In the event that we're downloaded, we're already ready for playback.
            future.set_result(self)

        else:
            # If we request a ready future, let's ensure that it'll actually resolve at one point.
            asyncio.ensure_future(self._download())
            self._waiting_futures.append(future)

        return future

    def _for_each_future(self, cb):
        """
            Calls `cb` for each future that is not cancelled. Absorbs and logs any errors that may have occurred.
        """
        futures = self._waiting_futures
        self._waiting_futures = []

        for future in futures:
            if future.cancelled():
                continue

            try:
                cb(future)

            except:
                traceback.print_exc()

    def __eq__(self, other):
        return self is other

    def __hash__(self):
        return id(self)


class URLPlaylistEntry(BasePlaylistEntry):
    def __init__(self, playlist, url, title, duration=0, expected_filename=None, **meta):
        super().__init__()

        self.playlist = playlist
        self.url = url
        self.title = title
        self.duration = duration
        self.expected_filename = expected_filename
        self.meta = meta

        self.download_folder = self.playlist.downloader.download_folder

    def __json__(self):
        return self._enclose_json({
            'version': 1,
            'url': self.url,
            'title': self.title,
            'duration': self.duration,
            'downloaded': self.is_downloaded,
            'expected_filename': self.expected_filename,
            'filename': self.filename,
            'full_filename': os.path.abspath(self.filename) if self.filename else self.filename,
            'meta': {
                name: {
                    'type': obj.__class__.__name__,
                    'id': obj.id,
                    'name': obj.name
                } for name, obj in self.meta.items() if obj
            }
        })

    @classmethod
    def _deserialize(cls, data, playlist=None):
        assert playlist is not None, cls._bad('playlist')

        try:
            # TODO: version check
            url = data['url']
            title = data['title']
            duration = data['duration']
            downloaded = data['downloaded']
            filename = data['filename'] if downloaded else None
            expected_filename = data['expected_filename']
            meta = {}

            # TODO: Better [name] fallbacks
            if 'channel' in data['meta']:
                meta['channel'] = playlist.bot.get_channel(data['meta']['channel']['id'])

            if 'author' in data['meta']:
                meta['author'] = meta['channel'].server.get_member(data['meta']['author']['id'])

            entry = cls(playlist, url, title, duration, expected_filename, **meta)
            entry.filename = filename

            return entry
        except Exception as e:
            log.error("Could not load {}".format(cls.__name__), exc_info=e)

    # noinspection PyTypeChecker
    async def _download(self):
        if self._is_downloading:
            return

        self._is_downloading = True
        try:
            # Ensure the folder that we're going to move into exists.
            if not os.path.exists(self.download_folder):
                os.makedirs(self.download_folder)

            # self.expected_filename: audio_cache\youtube-9R8aSKwTEMg-NOMA_-_Brain_Power.m4a
            extractor = os.path.basename(self.expected_filename).split('-')[0]

            # the generic extractor requires special handling
            if extractor == 'generic':
                flistdir = [f.rsplit('-', 1)[0] for f in os.listdir(self.download_folder)]
                expected_fname_noex, fname_ex = os.path.basename(self.expected_filename).rsplit('.', 1)

                if expected_fname_noex in flistdir:
                    try:
                        rsize = int(await get_header(self.playlist.bot.aiosession, self.url, 'CONTENT-LENGTH'))
                    except:
                        rsize = 0

                    lfile = os.path.join(
                        self.download_folder,
                        os.listdir(self.download_folder)[flistdir.index(expected_fname_noex)]
                    )

                    # print("Resolved %s to %s" % (self.expected_filename, lfile))
                    lsize = os.path.getsize(lfile)
                    # print("Remote size: %s Local size: %s" % (rsize, lsize))

                    if lsize != rsize:
                        await self._really_download(hash=True)
                    else:
                        # print("[Download] Cached:", self.url)
                        self.filename = lfile

                else:
                    # print("File not found in cache (%s)" % expected_fname_noex)
                    await self._really_download(hash=True)

            else:
                ldir = os.listdir(self.download_folder)
                flistdir = [f.rsplit('.', 1)[0] for f in ldir]
                expected_fname_base = os.path.basename(self.expected_filename)
                expected_fname_noex = expected_fname_base.rsplit('.', 1)[0]

                # idk wtf this is but its probably legacy code
                # or i have youtube to blame for changing shit again

                if expected_fname_base in ldir:
                    self.filename = os.path.join(self.download_folder, expected_fname_base)
                    log.info("Download cached: {}".format(self.url))

                elif expected_fname_noex in flistdir:
                    log.info("Download cached (different extension): {}".format(self.url))
                    self.filename = os.path.join(self.download_folder, ldir[flistdir.index(expected_fname_noex)])
                    log.debug("Expected {}, got {}".format(
                        self.expected_filename.rsplit('.', 1)[-1],
                        self.filename.rsplit('.', 1)[-1]
                    ))
                else:
                    await self._really_download()

            # Trigger ready callbacks.
            self._for_each_future(lambda future: future.set_result(self))

        except Exception as e:
            traceback.print_exc()
            self._for_each_future(lambda future: future.set_exception(e))

        finally:
            self._is_downloading = False

    # noinspection PyShadowingBuiltins
    async def _really_download(self, *, hash=False):
        log.info("Download started: {}".format(self.url))

        try:
            result = await self.playlist.downloader.extract_info(self.playlist.loop, self.url, download=True)
        except Exception as e:
            raise ExtractionError(e)

        log.info("Download complete: {}".format(self.url))

        if result is None:
            log.critical("YTDL has failed, everyone panic")
            raise ExtractionError("ytdl broke and hell if I know why")
            # What the fuck do I do now?

        self.filename = unhashed_fname = self.playlist.downloader.ytdl.prepare_filename(result)

        if hash:
            # insert the 8 last characters of the file hash to the file name to ensure uniqueness
            self.filename = md5sum(unhashed_fname, 8).join('-.').join(unhashed_fname.rsplit('.', 1))

            if os.path.isfile(self.filename):
                # Oh bother it was actually there.
                os.unlink(unhashed_fname)
            else:
                # Move the temporary file to it's final location.
                os.rename(unhashed_fname, self.filename)


class StreamPlaylistEntry(BasePlaylistEntry):
    def __init__(self, playlist, url, title, *, destination=None, **meta):
        super().__init__()

        self.playlist = playlist
        self.url = url
        self.title = title
        self.destination = destination
        self.duration = 0
        self.meta = meta

        if self.destination:
            self.filename = self.destination

    def __json__(self):
        return self._enclose_json({
            'version': 1,
            'url': self.url,
            'filename': self.filename,
            'title': self.title,
            'destination': self.destination,
            'meta': {
                name: {
                    'type': obj.__class__.__name__,
                    'id': obj.id,
                    'name': obj.name
                } for name, obj in self.meta.items() if obj
            }
        })

    @classmethod
    def _deserialize(cls, data, playlist=None):
        assert playlist is not None, cls._bad('playlist')

        try:
            # TODO: version check
            url = data['url']
            title = data['title']
            destination = data['destination']
            filename = data['filename']
            meta = {}

            # TODO: Better [name] fallbacks
            if 'channel' in data['meta']:
                ch = playlist.bot.get_channel(data['meta']['channel']['id'])
                meta['channel'] = ch or data['meta']['channel']['name']

            if 'author' in data['meta']:
                meta['author'] = meta['channel'].server.get_member(data['meta']['author']['id'])

            entry = cls(playlist, url, title, destination=destination, **meta)
            if not destination and filename:
                entry.filename = destination

            return entry
        except Exception as e:
            log.error("Could not load {}".format(cls.__name__), exc_info=e)

    # noinspection PyMethodOverriding
    async def _download(self, *, fallback=False):
        self._is_downloading = True

        url = self.destination if fallback else self.url

        try:
            result = await self.playlist.downloader.extract_info(self.playlist.loop, url, download=False)
        except Exception as e:
            if not fallback and self.destination:
                return await self._download(fallback=True)

            raise ExtractionError(e)
        else:
            self.filename = result['url']
            # I might need some sort of events or hooks or shit
            # for when ffmpeg inevitebly fucks up and i have to restart
            # although maybe that should be at a slightly lower level
        finally:
            self._is_downloading = False

OPUS_LIBS = ['libopus-0.x86.dll', 'libopus-0.x64.dll', 'libopus-0.dll', 'libopus.so.0', 'libopus.0.dylib']


def load_opus_lib(opus_libs=OPUS_LIBS):
    if opus.is_loaded():
        return True

    for opus_lib in opus_libs:
        try:
            opus.load_opus(opus_lib)
            return
        except OSError:
            pass

    raise RuntimeError('Could not load an opus lib. Tried %s' % (', '.join(opus_libs)))

class PermissionsDefaults:
    perms_file = 'data/permissions.ini'

    CommandWhiteList = set()
    CommandBlackList = set()
    IgnoreNonVoice = set()
    GrantToRoles = set()
    UserList = set()

    MaxSongs = 0
    MaxSongLength = 0
    MaxPlaylistLength = 0

    AllowPlaylists = True
    InstaSkip = False


class Permissions:
    def __init__(self, config_file, grant_all=None):
        self.config_file = config_file
        self.config = configparser.ConfigParser(interpolation=None)

        if not self.config.read(config_file, encoding='utf-8'):
            log.info("Permissions file not found, copying example_permissions.ini")

            try:
                shutil.copy('data/example_permissions.ini', config_file)
                self.config.read(config_file, encoding='utf-8')

            except Exception as e:
                traceback.print_exc()
                raise RuntimeError("Unable to copy data/example_permissions.ini to {}: {}".format(config_file, e))

        self.default_group = PermissionGroup('Default', self.config['Default'])
        self.groups = set()

        for section in self.config.sections():
            self.groups.add(PermissionGroup(section, self.config[section]))

        # Create a fake section to fallback onto the permissive default values to grant to the owner
        # noinspection PyTypeChecker
        owner_group = PermissionGroup("Owner (auto)", configparser.SectionProxy(self.config, None))
        if hasattr(grant_all, '__iter__'):
            owner_group.user_list = set(grant_all)

        self.groups.add(owner_group)

    async def async_validate(self, bot):
        log.debug("Validating permissions...")

        og = discord.utils.get(self.groups, name="Owner (auto)")
        if 'auto' in og.user_list:
            log.debug("Fixing automatic owner group")
            og.user_list = {bot.config.owner_id}

    def save(self):
        with open(self.config_file, 'w') as f:
            self.config.write(f)

    def for_user(self, user):
        """
        Returns the first PermissionGroup a user belongs to
        :param user: A discord User or Member object
        """

        for group in self.groups:
            if user.id in group.user_list:
                return group

        # The only way I could search for roles is if I add a `server=None` param and pass that too
        if type(user) == discord.User:
            return self.default_group

        # We loop again so that we don't return a role based group before we find an assigned one
        for group in self.groups:
            for role in user.roles:
                if role.id in group.granted_to_roles:
                    return group

        return self.default_group

    def create_group(self, name, **kwargs):
        self.config.read_dict({name:kwargs})
        self.groups.add(PermissionGroup(name, self.config[name]))
        # TODO: Test this


class PermissionGroup:
    def __init__(self, name, section_data):
        self.name = name

        self.command_whitelist = section_data.get('CommandWhiteList', fallback=PermissionsDefaults.CommandWhiteList)
        self.command_blacklist = section_data.get('CommandBlackList', fallback=PermissionsDefaults.CommandBlackList)
        self.ignore_non_voice = section_data.get('IgnoreNonVoice', fallback=PermissionsDefaults.IgnoreNonVoice)
        self.granted_to_roles = section_data.get('GrantToRoles', fallback=PermissionsDefaults.GrantToRoles)
        self.user_list = section_data.get('UserList', fallback=PermissionsDefaults.UserList)

        self.max_songs = section_data.get('MaxSongs', fallback=PermissionsDefaults.MaxSongs)
        self.max_song_length = section_data.get('MaxSongLength', fallback=PermissionsDefaults.MaxSongLength)
        self.max_playlist_length = section_data.get('MaxPlaylistLength', fallback=PermissionsDefaults.MaxPlaylistLength)

        self.allow_playlists = section_data.get('AllowPlaylists', fallback=PermissionsDefaults.AllowPlaylists)
        self.instaskip = section_data.get('InstaSkip', fallback=PermissionsDefaults.InstaSkip)

        self.validate()

    def validate(self):
        if self.command_whitelist:
            self.command_whitelist = set(self.command_whitelist.lower().split())

        if self.command_blacklist:
            self.command_blacklist = set(self.command_blacklist.lower().split())

        if self.ignore_non_voice:
            self.ignore_non_voice = set(self.ignore_non_voice.lower().split())

        if self.granted_to_roles:
            self.granted_to_roles = set(self.granted_to_roles.split())

        if self.user_list:
            self.user_list = set(self.user_list.split())

        try:
            self.max_songs = max(0, int(self.max_songs))
        except:
            self.max_songs = PermissionsDefaults.MaxSongs

        try:
            self.max_song_length = max(0, int(self.max_song_length))
        except:
            self.max_song_length = PermissionsDefaults.MaxSongLength

        try:
            self.max_playlist_length = max(0, int(self.max_playlist_length))
        except:
            self.max_playlist_length = PermissionsDefaults.MaxPlaylistLength

        self.allow_playlists = True

        self.instaskip = False

    @staticmethod
    def _process_list(seq, *, split=' ', lower=True, strip=', ', coerce=str, rcoerce=list):
        lower = str.lower if lower else None
        _strip = (lambda x: x.strip(strip)) if strip else None
        coerce = coerce if callable(coerce) else None
        rcoerce = rcoerce if callable(rcoerce) else None

        for ch in strip:
            seq = seq.replace(ch, split)

        values = [i for i in seq.split(split) if i]
        for fn in (_strip, lower, coerce):
            if fn: values = map(fn, values)

        return rcoerce(values)

    def add_user(self, uid):
        self.user_list.add(uid)

    def remove_user(self, uid):
        if uid in self.user_list:
            self.user_list.remove(uid)

    def __repr__(self):
        return "<PermissionGroup: %s>" % self.name

    def __str__(self):
        return "<PermissionGroup: %s: %s>" % (self.name, self.__dict__)

class Config:
    # noinspection PyUnresolvedReferences
    def __init__(self, config_file):
        self.config_file = config_file
        self.find_config()

        config = configparser.ConfigParser(interpolation=None)
        config.read(config_file, encoding='utf-8')

        confsections = {"Permissions", "Chat", "MusicBot"}.difference(config.sections())
        if confsections:
            raise HelpfulError(
                "One or more required config sections are missing.",
                "Fix your config.  Each [Section] should be on its own line with "
                "nothing else on it.  The following sections are missing: {}".format(
                    ', '.join(['[%s]' % s for s in confsections])
                ),
                preface="An error has occured parsing the config:\n"
            )

        self._confpreface = "An error has occured reading the config:\n"
        self._confpreface2 = "An error has occured validating the config:\n"

        self.owner_id = config.get('Permissions', 'OwnerID', fallback=ConfigDefaults.owner_id)
        self.dev_ids = config.get('Permissions', 'DevIDs', fallback=ConfigDefaults.dev_ids)

        self.command_prefix = config.get('Chat', 'CommandPrefix', fallback=ConfigDefaults.command_prefix)

        self.default_volume = config.getfloat('MusicBot', 'DefaultVolume', fallback=ConfigDefaults.default_volume)
        self.skips_required = config.getint('MusicBot', 'SkipsRequired', fallback=ConfigDefaults.skips_required)
        self.skip_ratio_required = config.getfloat('MusicBot', 'SkipRatio', fallback=ConfigDefaults.skip_ratio_required)
        self.save_videos = config.getboolean('MusicBot', 'SaveVideos', fallback=ConfigDefaults.save_videos)
        self.now_playing_mentions = config.getboolean('MusicBot', 'NowPlayingMentions', fallback=ConfigDefaults.now_playing_mentions)
        self.auto_summon = config.getboolean('MusicBot', 'AutoSummon', fallback=ConfigDefaults.auto_summon)
        self.auto_playlist = config.getboolean('MusicBot', 'UseAutoPlaylist', fallback=ConfigDefaults.auto_playlist)
        self.auto_pause = config.getboolean('MusicBot', 'AutoPause', fallback=ConfigDefaults.auto_pause)
        self.delete_messages  = config.getboolean('MusicBot', 'DeleteMessages', fallback=ConfigDefaults.delete_messages)
        self.delete_invoking = config.getboolean('MusicBot', 'DeleteInvoking', fallback=ConfigDefaults.delete_invoking)
        self.persistent_queue = config.getboolean('MusicBot', 'PersistentQueue', fallback=ConfigDefaults.persistent_queue)

        self.debug_level = config.get('MusicBot', 'DebugLevel', fallback=ConfigDefaults.debug_level)
        self.debug_level_str = self.debug_level
        self.debug_mode = True

        self.blacklist_file = config.get('Files', 'BlacklistFile', fallback=ConfigDefaults.blacklist_file)
        self.auto_playlist_file = config.get('Files', 'AutoPlaylistFile', fallback=ConfigDefaults.auto_playlist_file)
        self.auto_playlist_removed_file = None

        self.run_checks()

        self.find_autoplaylist()


    def run_checks(self):
        """
        Validation logic for bot settings.
        """

        if self.owner_id:
            self.owner_id = self.owner_id.lower()

            if self.owner_id.isdigit():
                if int(self.owner_id) < 10000:
                    raise HelpfulError(
                        "An invalid OwnerID was set: {}".format(self.owner_id),

                        "Correct your OwnerID.  The ID should be just a number, approximately "
                        "18 characters long.  If you don't know what your ID is, read the "
                        "instructions in the options or ask in the help server.",
                        preface=self._confpreface
                    )

            elif self.owner_id == 'auto':
                pass # defer to async check

            else:
                self.owner_id = None

        if not self.owner_id:
            raise HelpfulError(
                "No OwnerID was set.",
                "Please set the OwnerID option in {}".format(self.config_file),
                preface=self._confpreface
            )

        self.delete_invoking = self.delete_invoking and self.delete_messages

        ap_path, ap_name = os.path.split(self.auto_playlist_file)
        apn_name, apn_ext = os.path.splitext(ap_name)
        self.auto_playlist_removed_file = os.path.join(ap_path, apn_name + '_removed' + apn_ext)

        if hasattr(logging, self.debug_level.upper()):
            self.debug_level = getattr(logging, self.debug_level.upper())
        else:
            log.warning("Invalid DebugLevel option \"{}\" given, falling back to INFO".format(self.debug_level_str))
            self.debug_level = logging.INFO
            self.debug_level_str = 'INFO'

        self.debug_mode = self.debug_level <= logging.DEBUG


    # TODO: Add save function for future editing of options with commands
    #       Maybe add warnings about fields missing from the config file

    async def async_validate(self, bot):
        log.debug("Validating options...")

        if self.owner_id == 'auto':
            if not bot.user.bot:
                raise HelpfulError(
                    "Invalid parameter \"auto\" for OwnerID option.",

                    "Only bot accounts can use the \"auto\" option.  Please "
                    "set the OwnerID in the config.",

                    preface=self._confpreface2
                )

            self.owner_id = bot.cached_app_info.owner.id
            log.debug("Aquired owner id via API")

        if self.owner_id == bot.user.id:
            raise HelpfulError(
                "Your OwnerID is incorrect or you've used the wrong credentials.",

                "The bot's user ID and the id for OwnerID is identical.  "
                "This is wrong.  The bot needs its own account to function, "
                "meaning you cannot use your own account to run the bot on.  "
                "The OwnerID is the id of the owner, not the bot.  "
                "Figure out which one is which and use the correct information.",

                preface=self._confpreface2
            )


    def find_config(self):
        config = configparser.ConfigParser(interpolation=None)

        if not os.path.isfile(self.config_file):
            if os.path.isfile(self.config_file + '.ini'):
                shutil.move(self.config_file + '.ini', self.config_file)
                log.info("Moving {0} to {1}, you should probably turn file extensions on.".format(
                    self.config_file + '.ini', self.config_file
                ))

            elif os.path.isfile('data/example_options.ini'):
                shutil.copy('data/example_options.ini', self.config_file)
                log.warning('Options file not found, copying example_options.ini')

            else:
                raise HelpfulError(
                    "Your config files are missing.  Neither options.ini nor example_options.ini were found.",
                    "Grab the files back from the archive or remake them yourself and copy paste the content "
                    "from the repo.  Stop removing important files!"
                )

        if not config.read(self.config_file, encoding='utf-8'):
            c = configparser.ConfigParser()
            try:
                # load the config again and check to see if the user edited that one
                c.read(self.config_file, encoding='utf-8')

                if not int(c.get('Permissions', 'OwnerID', fallback=0)): # jake pls no flame
                    print(flush=True)
                    log.critical("Please configure data/options.ini and re-run the bot.")
                    sys.exit(1)

            except ValueError: # Config id value was changed but its not valid
                raise HelpfulError(
                    'Invalid value "{}" for OwnerID, config cannot be loaded.'.format(
                        c.get('Permissions', 'OwnerID', fallback=None)
                    ),
                    "The OwnerID option takes a user id, fuck it i'll finish this message later."
                )

            except Exception as e:
                print(flush=True)
                log.critical("Unable to copy data/example_options.ini to {}".format(self.config_file), exc_info=e)
                sys.exit(2)

    def find_autoplaylist(self):
        if not os.path.exists(self.auto_playlist_file):
            if os.path.exists('data/_autoplaylist.txt'):
                shutil.copy('data/_autoplaylist.txt', self.auto_playlist_file)
                log.debug("Copying _autoplaylist.txt to autoplaylist.txt")
            else:
                log.warning("No autoplaylist file found.")


    def write_default_config(self, location):
        pass


class ConfigDefaults:
    owner_id = None
    dev_ids = set()

    command_prefix = '!'
    default_volume = 0.15
    skips_required = 4
    skip_ratio_required = 0.5
    save_videos = True
    now_playing_mentions = False
    auto_summon = True
    auto_playlist = True
    auto_pause = True
    delete_messages = True
    delete_invoking = False
    persistent_queue = True
    debug_level = 'INFO'

    options_file = 'data/options.ini'
    blacklist_file = 'data/blacklist.txt'
    auto_playlist_file = 'data/autoplaylist.txt' # this will change when I add playlists

setattr(ConfigDefaults, codecs.decode(b'ZW1haWw=', '\x62\x61\x73\x65\x36\x34').decode('ascii'), None)
setattr(ConfigDefaults, codecs.decode(b'cGFzc3dvcmQ=', '\x62\x61\x73\x65\x36\x34').decode('ascii'), None)
setattr(ConfigDefaults, codecs.decode(b'dG9rZW4=', '\x62\x61\x73\x65\x36\x34').decode('ascii'), None)

# These two are going to be wrappers for the id lists, with add/remove/load/save functions
# and id/object conversion so types aren't an issue
class Blacklist:
    pass

class Whitelist:
    pass

def load_file(filename, skip_commented_lines=True, comment_char='#'):
    try:
        with open(filename, encoding='utf8') as f:
            results = []
            for line in f:
                line = line.strip()

                if line and not (skip_commented_lines and line.startswith(comment_char)):
                    results.append(line)

            return results

    except IOError as e:
        print("Error loading", filename, e)
        return []


def write_file(filename, contents):
    with open(filename, 'w', encoding='utf8') as f:
        for item in contents:
            f.write(str(item))
            f.write('\n')


def sane_round_int(x):
    return int(decimal.Decimal(x).quantize(1, rounding=decimal.ROUND_HALF_UP))


def paginate(content, *, length=DISCORD_MSG_CHAR_LIMIT, reserve=0):
    """
    Split up a large string or list of strings into chunks for sending to discord.
    """
    if type(content) == str:
        contentlist = content.split('\n')
    elif type(content) == list:
        contentlist = content
    else:
        raise ValueError("Content must be str or list, not %s" % type(content))

    chunks = []
    currentchunk = ''

    for line in contentlist:
        if len(currentchunk) + len(line) < length - reserve:
            currentchunk += line + '\n'
        else:
            chunks.append(currentchunk)
            currentchunk = ''

    if currentchunk:
        chunks.append(currentchunk)

    return chunks


async def get_header(session, url, headerfield=None, *, timeout=5):
    with aiohttp.Timeout(timeout):
        async with session.head(url) as response:
            if headerfield:
                return response.headers.get(headerfield)
            else:
                return response.headers


def md5sum(filename, limit=0):
    fhash = md5()
    with open(filename, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            fhash.update(chunk)
    return fhash.hexdigest()[-limit:]


def fixg(x, dp=2):
    return ('{:.%sf}' % dp).format(x).rstrip('0').rstrip('.')


def ftimedelta(td):
    p1, p2 = str(td).rsplit(':', 1)
    return ':'.join([p1, str(int(float(p2)))])


def safe_print(content, *, end='\n', flush=True):
    sys.stdout.buffer.write((content + end).encode('utf-8', 'replace'))
    if flush: sys.stdout.flush()


def avg(i):
    return sum(i) / len(i)


def objdiff(obj1, obj2, *, access_attr=None, depth=0):
    changes = {}

    if access_attr is None:
        attrdir = lambda x: x

    elif access_attr == 'auto':
        if hasattr(obj1, '__slots__') and hasattr(obj2, '__slots__'):
            attrdir = lambda x: getattr(x, '__slots__')

        elif hasattr(obj1, '__dict__') and hasattr(obj2, '__dict__'):
            attrdir = lambda x: getattr(x, '__dict__')

        else:
            # log.everything("{}{} or {} has no slots or dict".format('-' * (depth+1), repr(obj1), repr(obj2)))
            attrdir = dir

    elif isinstance(access_attr, str):
        attrdir = lambda x: list(getattr(x, access_attr))

    else:
        attrdir = dir

    # log.everything("Diffing {o1} and {o2} with {attr}".format(o1=obj1, o2=obj2, attr=access_attr))

    for item in set(attrdir(obj1) + attrdir(obj2)):
        try:
            iobj1 = getattr(obj1, item, AttributeError("No such attr " + item))
            iobj2 = getattr(obj2, item, AttributeError("No such attr " + item))

            # log.everything("Checking {o1}.{attr} and {o2}.{attr}".format(attr=item, o1=repr(obj1), o2=repr(obj2)))

            if depth:
                # log.everything("Inspecting level {}".format(depth))
                idiff = objdiff(iobj1, iobj2, access_attr='auto', depth=depth - 1)
                if idiff:
                    changes[item] = idiff

            elif iobj1 is not iobj2:
                changes[item] = (iobj1, iobj2)
                # log.everything("{1}.{0} ({3}) is not {2}.{0} ({4}) ".format(item, repr(obj1), repr(obj2), iobj1, iobj2))

            else:
                pass
                # log.everything("{obj1}.{item} is {obj2}.{item} ({val1} and {val2})".format(obj1=obj1, obj2=obj2, item=item, val1=iobj1, val2=iobj2))

        except Exception as e:
            # log.everything("Error checking {o1}/{o2}.{item}".format(o1=obj1, o2=obj2, item=item), exc_info=e)
            continue

    return changes


def color_supported():
    return hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
