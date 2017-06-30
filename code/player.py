import os
import sys
import json
import logging
import asyncio
import audioop
import subprocess
import datetime
import functools
import youtube_dl

from enum import Enum
from array import array
from threading import Thread
from collections import deque
from shutil import get_terminal_size
from websockets.exceptions import InvalidState
from random import shuffle
from itertools import islice
from collections import deque
from urllib.error import URLError
from concurrent.futures import ThreadPoolExecutor

from discord.http import _func_

from .core import avg
from .lib.event_emitter import EventEmitter
from .core import Serializable, Serializer, URLPlaylistEntry, StreamPlaylistEntry, ExtractionError, WrongEntryTypeError, FFmpegError, FFmpegWarning, Serializable, get_header
from youtube_dl.utils import ExtractorError, DownloadError, UnsupportedError
log = logging.getLogger(__name__)


class PatchedBuff:
    """
        PatchedBuff monkey patches a readable object, allowing you to vary what the volume is as the song is playing.
    """

    def __init__(self, buff, *, draw=False):
        self.buff = buff
        self.frame_count = 0
        self.volume = 1.0

        self.draw = draw
        self.use_audioop = True
        self.frame_skip = 2
        self.rmss = deque([2048], maxlen=90)

    def __del__(self):
        if self.draw:
            print(' ' * (get_terminal_size().columns-1), end='\r')

    def read(self, frame_size):
        self.frame_count += 1

        frame = self.buff.read(frame_size)

        if self.volume != 1:
            frame = self._frame_vol(frame, self.volume, maxv=2)

        if self.draw and not self.frame_count % self.frame_skip:
            # these should be processed for every frame, but "overhead"
            rms = audioop.rms(frame, 2)
            self.rmss.append(rms)

            max_rms = sorted(self.rmss)[-1]
            meter_text = 'avg rms: {:.2f}, max rms: {:.2f} '.format(avg(self.rmss), max_rms)
            self._pprint_meter(rms / max(1, max_rms), text=meter_text, shift=True)

        return frame

    def _frame_vol(self, frame, mult, *, maxv=2, use_audioop=True):
        if use_audioop:
            return audioop.mul(frame, 2, min(mult, maxv))
        else:
            # ffmpeg returns s16le pcm frames.
            frame_array = array('h', frame)

            for i in range(len(frame_array)):
                frame_array[i] = int(frame_array[i] * min(mult, min(1, maxv)))

            return frame_array.tobytes()

    def _pprint_meter(self, perc, *, char='#', text='', shift=True):
        tx, ty = get_terminal_size()

        if shift:
            outstr = text + "{}".format(char * (int((tx - len(text)) * perc) - 1))
        else:
            outstr = text + "{}".format(char * (int(tx * perc) - 1))[len(text):]

        print(outstr.ljust(tx - 1), end='\r')


class MusicPlayerState(Enum):
    STOPPED = 0  # When the player isn't playing anything
    PLAYING = 1  # The player is actively playing music.
    PAUSED = 2   # The player is paused on a song.
    WAITING = 3  # The player has finished its song but is still downloading the next one
    DEAD = 4     # The player has been killed.

    def __str__(self):
        return self.name


class MusicPlayer(EventEmitter, Serializable):
    def __init__(self, bot, voice_client, playlist):
        super().__init__()
        self.bot = bot
        self.loop = bot.loop
        self.voice_client = voice_client
        self.playlist = playlist
        self.state = MusicPlayerState.STOPPED
        self.skip_state = None

        self._volume = bot.config.default_volume
        self._play_lock = asyncio.Lock()
        self._current_player = None
        self._current_entry = None
        self._stderr_future = None

        self.playlist.on('entry-added', self.on_entry_added)
        self.loop.create_task(self.websocket_check())

    @property
    def volume(self):
        return self._volume

    @volume.setter
    def volume(self, value):
        self._volume = value
        if self._current_player:
            self._current_player.buff.volume = value

    def on_entry_added(self, playlist, entry):
        if self.is_stopped:
            self.loop.call_later(2, self.play)

        self.emit('entry-added', player=self, playlist=playlist, entry=entry)

    def skip(self):
        self._kill_current_player()

    def stop(self):
        self.state = MusicPlayerState.STOPPED
        self._kill_current_player()

        self.emit('stop', player=self)

    def resume(self):
        if self.is_paused and self._current_player:
            self._current_player.resume()
            self.state = MusicPlayerState.PLAYING
            self.emit('resume', player=self, entry=self.current_entry)
            return

        if self.is_paused and not self._current_player:
            self.state = MusicPlayerState.PLAYING
            self._kill_current_player()
            return

        raise ValueError('Cannot resume playback from state %s' % self.state)

    def pause(self):
        if self.is_playing:
            self.state = MusicPlayerState.PAUSED

            if self._current_player:
                self._current_player.pause()

            self.emit('pause', player=self, entry=self.current_entry)
            return

        elif self.is_paused:
            return

        raise ValueError('Cannot pause a MusicPlayer in state %s' % self.state)

    def kill(self):
        self.state = MusicPlayerState.DEAD
        self.playlist.clear()
        self._events.clear()
        self._kill_current_player()

    def _playback_finished(self):
        entry = self._current_entry

        if self._current_player:
            self._current_player.after = None
            self._kill_current_player()

        self._current_entry = None

        if self._stderr_future.done() and self._stderr_future.exception():
            # I'm not sure that this would ever not be done if it gets to this point
            # unless ffmpeg is doing something highly questionable
            self.emit('error', player=self, entry=entry, ex=self._stderr_future.exception())

        if not self.is_stopped and not self.is_dead:
            self.play(_continue=True)

        if not self.bot.config.save_videos and entry:
            if any([entry.filename == e.filename for e in self.playlist.entries]):
                log.debug("Skipping deletion of \"{}\", found song in queue".format(entry.filename))

            else:
                log.debug("Deleting file: {}".format(os.path.relpath(entry.filename)))
                asyncio.ensure_future(self._delete_file(entry.filename))

        self.emit('finished-playing', player=self, entry=entry)

    def _kill_current_player(self):
        if self._current_player:
            if self.is_paused:
                self.resume()

            try:
                self._current_player.stop()
            except OSError:
                pass
            self._current_player = None
            return True

        return False

    async def _delete_file(self, filename):
        for x in range(30):
            try:
                os.unlink(filename)
                break
            except FileNotFoundError:
                pass
            except PermissionError as e:
                if e.winerror == 32:  # File is in use
                    await asyncio.sleep(0.25)

            except Exception:
                log.error("Error trying to delete {}".format(filename), exc_info=True)
                break
        else:
            print("[Config:SaveVideos] Could not delete file {}, giving up and moving on".format(
                os.path.relpath(filename)))



    def play(self, _continue=False):
        self.loop.create_task(self._play(_continue=_continue))

    async def _play(self, _continue=False):
        """
            Plays the next entry from the playlist, or resumes playback of the current entry if paused.
        """
        if self.is_paused:
            return self.resume()

        if self.is_dead:
            return

        with await self._play_lock:
            if self.is_stopped or _continue:
                try:
                    entry = await self.playlist.get_next_entry()

                except:
                    log.warning("Failed to get entry, retrying", exc_info=True)
                    self.loop.call_later(0.1, self.play)
                    return

                # If nothing left to play, transition to the stopped state.
                if not entry:
                    self.stop()
                    return

                # In-case there was a player, kill it. RIP.
                self._kill_current_player()

                boptions = "-nostdin"
                # aoptions = "-vn -b:a 192k"
                aoptions = "-vn"

                log.ffmpeg("Creating player with options: {} {} {}".format(boptions, aoptions, entry.filename))
                self._current_player = self._monkeypatch_player(self.voice_client.create_ffmpeg_player(
                    entry.filename,
                    before_options=boptions,
                    options=aoptions,
                    stderr=subprocess.PIPE,
                    # Threadsafe call soon, b/c after will be called from the voice playback thread.
                    after=lambda: self.loop.call_soon_threadsafe(self._playback_finished)
                ))
                self._current_player.setDaemon(True)
                self._current_player.buff.volume = self.volume

                # I need to add ytdl hooks
                self.state = MusicPlayerState.PLAYING
                self._current_entry = entry
                self._stderr_future = asyncio.Future()

                stderr_thread = Thread(
                    target=filter_stderr,
                    args=(self._current_player.process, self._stderr_future),
                    name="{} stderr reader".format(self._current_player.name)
                )

                stderr_thread.start()
                self._current_player.start()

                self.emit('play', player=self, entry=entry)

    def _monkeypatch_player(self, player):
        original_buff = player.buff
        player.buff = PatchedBuff(original_buff)
        return player

    async def reload_voice(self, voice_client):
        async with self.bot.aiolocks[_func_() + ':' + voice_client.channel.server.id]:
            self.voice_client = voice_client
            if self._current_player:
                self._current_player.player = voice_client.play_audio
                self._current_player._resumed.clear()
                self._current_player._connected.set()

    async def websocket_check(self):
        log.voicedebug("Starting websocket check loop for {}".format(self.voice_client.channel.server))

        while not self.is_dead:
            try:
                async with self.bot.aiolocks[self.reload_voice.__name__ + ':' + self.voice_client.channel.server.id]:
                    await self.voice_client.ws.ensure_open()

            except InvalidState:
                log.debug("Voice websocket for \"{}\" is {}, reconnecting".format(
                    self.voice_client.channel.server,
                    self.voice_client.ws.state_name
                ))
                await self.bot.reconnect_voice_client(self.voice_client.channel.server, channel=self.voice_client.channel)
                await asyncio.sleep(3)

            except Exception:
                log.error("Error in websocket check loop", exc_info=True)

            finally:
                await asyncio.sleep(1)

    def __json__(self):
        return self._enclose_json({
            'current_entry': {
                'entry': self.current_entry,
                'progress': self.progress,
                'progress_frames': self._current_player.buff.frame_count if self.progress is not None else None
            },
            'entries': self.playlist
        })

    @classmethod
    def _deserialize(cls, data, bot=None, voice_client=None, playlist=None):
        assert bot is not None, cls._bad('bot')
        assert voice_client is not None, cls._bad('voice_client')
        assert playlist is not None, cls._bad('playlist')

        player = cls(bot, voice_client, playlist)

        data_pl = data.get('entries')
        if data_pl and data_pl.entries:
            player.playlist.entries = data_pl.entries

        current_entry_data = data['current_entry']
        if current_entry_data['entry']:
            player.playlist.entries.appendleft(current_entry_data['entry'])
            # TODO: progress stuff
            # how do I even do this
            # this would have to be in the entry class right?
            # some sort of progress indicator to skip ahead with ffmpeg (however that works, reading and ignoring frames?)

        return player

    @classmethod
    def from_json(cls, raw_json, bot, voice_client, playlist):
        try:
            return json.loads(raw_json, object_hook=Serializer.deserialize)
        except:
            log.exception("Failed to deserialize player")


    @property
    def current_entry(self):
        return self._current_entry

    @property
    def is_playing(self):
        return self.state == MusicPlayerState.PLAYING

    @property
    def is_paused(self):
        return self.state == MusicPlayerState.PAUSED

    @property
    def is_stopped(self):
        return self.state == MusicPlayerState.STOPPED

    @property
    def is_dead(self):
        return self.state == MusicPlayerState.DEAD

    @property
    def progress(self):
        if self._current_player:
            return round(self._current_player.buff.frame_count * 0.02)
            # TODO: Properly implement this
            #       Correct calculation should be bytes_read/192k
            #       192k AKA sampleRate * (bitDepth / 8) * channelCount
            #       Change frame_count to bytes_read in the PatchedBuff

# TODO: I need to add a check for if the eventloop is closed

def filter_stderr(popen:subprocess.Popen, future:asyncio.Future):
    last_ex = None

    while True:
        data = popen.stderr.readline()
        if data:
            log.ffmpeg("Data from ffmpeg: {}".format(data))
            try:
                if check_stderr(data):
                    sys.stderr.buffer.write(data)
                    sys.stderr.buffer.flush()

            except FFmpegError as e:
                log.ffmpeg("Error from ffmpeg: %s", str(e).strip())
                last_ex = e

            except FFmpegWarning:
                pass # useless message
        else:
            break

    if last_ex:
        future.set_exception(last_ex)
    else:
        future.set_result(True)

def check_stderr(data:bytes):
    try:
        data = data.decode('utf8')
    except:
        log.ffmpeg("Unknown error decoding message from ffmpeg", exc_info=True)
        return True # fuck it

    # log.ffmpeg("Decoded data from ffmpeg: {}".format(data))

    # TODO: Regex
    warnings = [
        "Header missing",
        "Estimating duration from birate, this may be inaccurate",
        "Application provided invalid, non monotonically increasing dts to muxer in stream",
        "Last message repeated",
        "Failed to send close message",
        "decode_band_types: Input buffer exhausted before END element found"
    ]
    errors = [
        "Invalid data found when processing input", # need to regex this properly, its both a warning and an error
    ]

    if any(msg in data for msg in warnings):
        raise FFmpegWarning(data)

    if any(msg in data for msg in errors):
        raise FFmpegError(data)

    return True


class Playlist(EventEmitter, Serializable):
    """
        A playlist is manages the list of songs that will be played.
    """

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.loop = bot.loop
        self.downloader = bot.downloader
        self.entries = deque()

    def __iter__(self):
        return iter(self.entries)

    def __len__(self):
        return len(self.entries)

    def shuffle(self):
        shuffle(self.entries)

    def clear(self):
        self.entries.clear()

    async def add_entry(self, song_url, **meta):
        """
            Validates and adds a song_url to be played. This does not start the download of the song.

            Returns the entry & the position it is in the queue.

            :param song_url: The song url to add to the playlist.
            :param meta: Any additional metadata to add to the playlist entry.
        """

        try:
            info = await self.downloader.extract_info(self.loop, song_url, download=False)
        except Exception as e:
            raise ExtractionError('Could not extract information from {}\n\n{}'.format(song_url, e))

        if not info:
            raise ExtractionError('Could not extract information from %s' % song_url)

        # TODO: Sort out what happens next when this happens
        if info.get('_type', None) == 'playlist':
            raise WrongEntryTypeError("This is a playlist.", True, info.get('webpage_url', None) or info.get('url', None))

        if info.get('is_live', False):
            return await self.add_stream_entry(song_url, info=info, **meta)

        # TODO: Extract this to its own function
        if info['extractor'] in ['generic', 'Dropbox']:
            try:
                headers = await get_header(self.bot.aiosession, info['url'])
                content_type = headers.get('CONTENT-TYPE')
                log.debug("Got content type {}".format(content_type))

            except Exception as e:
                log.warning("Failed to get content type for url {} ({})".format(song_url, e))
                content_type = None

            if content_type:
                if content_type.startswith(('application/', 'image/')):
                    if not any(x in content_type for x in ('/ogg', '/octet-stream')):
                        # How does a server say `application/ogg` what the actual fuck
                        raise ExtractionError("Invalid content type \"%s\" for url %s" % (content_type, song_url))

                elif content_type.startswith('text/html'):
                    log.warning("Got text/html for content-type, this might be a stream")
                    pass # TODO: Check for shoutcast/icecast

                elif not content_type.startswith(('audio/', 'video/')):
                    log.warning("Questionable content-type \"{}\" for url {}".format(content_type, song_url))

        entry = URLPlaylistEntry(
            self,
            song_url,
            info.get('title', 'Untitled'),
            info.get('duration', 0) or 0,
            self.downloader.ytdl.prepare_filename(info),
            **meta
        )
        self._add_entry(entry)
        return entry, len(self.entries)

    async def add_stream_entry(self, song_url, info=None, **meta):
        if info is None:
            info = {'title': song_url, 'extractor': None}

            try:
                info = await self.downloader.extract_info(self.loop, song_url, download=False)

            except DownloadError as e:
                if e.exc_info[0] == UnsupportedError: # ytdl doesn't like it but its probably a stream
                    log.debug("Assuming content is a direct stream")

                elif e.exc_info[0] == URLError:
                    if os.path.exists(os.path.abspath(song_url)):
                        raise ExtractionError("This is not a stream, this is a file path.")

                    else: # it might be a file path that just doesn't exist
                        raise ExtractionError("Invalid input: {0.exc_info[0]}: {0.exc_info[1].reason}".format(e))

                else:
                    # traceback.print_exc()
                    raise ExtractionError("Unknown error: {}".format(e))

            except Exception as e:
                log.error('Could not extract information from {} ({}), falling back to direct'.format(song_url, e), exc_info=True)

        dest_url = song_url
        if info.get('extractor'):
            dest_url = info.get('url')

        if info.get('extractor', None) == 'twitch:stream': # may need to add other twitch types
            title = info.get('description')
        else:
            title = info.get('title', 'Untitled')

        # TODO: A bit more validation, "~stream some_url" should not just say :ok_hand:

        entry = StreamPlaylistEntry(
            self,
            song_url,
            title,
            destination = dest_url,
            **meta
        )
        self._add_entry(entry)
        return entry, len(self.entries)

    async def import_from(self, playlist_url, **meta):
        """
            Imports the songs from `playlist_url` and queues them to be played.

            Returns a list of `entries` that have been enqueued.

            :param playlist_url: The playlist url to be cut into individual urls and added to the playlist
            :param meta: Any additional metadata to add to the playlist entry
        """
        position = len(self.entries) + 1
        entry_list = []

        try:
            info = await self.downloader.safe_extract_info(self.loop, playlist_url, download=False)
        except Exception as e:
            raise ExtractionError('Could not extract information from {}\n\n{}'.format(playlist_url, e))

        if not info:
            raise ExtractionError('Could not extract information from %s' % playlist_url)

        # Once again, the generic extractor fucks things up.
        if info.get('extractor', None) == 'generic':
            url_field = 'url'
        else:
            url_field = 'webpage_url'

        baditems = 0
        for item in info['entries']:
            if item:
                try:
                    entry = URLPlaylistEntry(
                        self,
                        item[url_field],
                        item.get('title', 'Untitled'),
                        item.get('duration', 0) or 0,
                        self.downloader.ytdl.prepare_filename(item),
                        **meta
                    )

                    self._add_entry(entry)
                    entry_list.append(entry)
                except Exception as e:
                    baditems += 1
                    log.warning("Could not add item", exc_info=e)
                    log.debug("Item: {}".format(item), exc_info=True)
            else:
                baditems += 1

        if baditems:
            log.info("Skipped {} bad entries".format(baditems))

        return entry_list, position

    async def async_process_youtube_playlist(self, playlist_url, **meta):
        """
            Processes youtube playlists links from `playlist_url` in a questionable, async fashion.

            :param playlist_url: The playlist url to be cut into individual urls and added to the playlist
            :param meta: Any additional metadata to add to the playlist entry
        """

        try:
            info = await self.downloader.safe_extract_info(self.loop, playlist_url, download=False, process=False)
        except Exception as e:
            raise ExtractionError('Could not extract information from {}\n\n{}'.format(playlist_url, e))

        if not info:
            raise ExtractionError('Could not extract information from %s' % playlist_url)

        gooditems = []
        baditems = 0

        for entry_data in info['entries']:
            if entry_data:
                baseurl = info['webpage_url'].split('playlist?list=')[0]
                song_url = baseurl + 'watch?v=%s' % entry_data['id']

                try:
                    entry, elen = await self.add_entry(song_url, **meta)
                    gooditems.append(entry)

                except ExtractionError:
                    baditems += 1

                except Exception as e:
                    baditems += 1
                    log.error("Error adding entry {}".format(entry_data['id']), exc_info=e)
            else:
                baditems += 1

        if baditems:
            log.info("Skipped {} bad entries".format(baditems))

        return gooditems

    async def async_process_sc_bc_playlist(self, playlist_url, **meta):
        """
            Processes soundcloud set and bancdamp album links from `playlist_url` in a questionable, async fashion.

            :param playlist_url: The playlist url to be cut into individual urls and added to the playlist
            :param meta: Any additional metadata to add to the playlist entry
        """

        try:
            info = await self.downloader.safe_extract_info(self.loop, playlist_url, download=False, process=False)
        except Exception as e:
            raise ExtractionError('Could not extract information from {}\n\n{}'.format(playlist_url, e))

        if not info:
            raise ExtractionError('Could not extract information from %s' % playlist_url)

        gooditems = []
        baditems = 0

        for entry_data in info['entries']:
            if entry_data:
                song_url = entry_data['url']

                try:
                    entry, elen = await self.add_entry(song_url, **meta)
                    gooditems.append(entry)

                except ExtractionError:
                    baditems += 1

                except Exception as e:
                    baditems += 1
                    log.error("Error adding entry {}".format(entry_data['id']), exc_info=e)
            else:
                baditems += 1

        if baditems:
            log.info("Skipped {} bad entries".format(baditems))

        return gooditems

    def _add_entry(self, entry, *, head=False):
        if head:
            self.entries.appendleft(entry)
        else:
            self.entries.append(entry)

        self.emit('entry-added', playlist=self, entry=entry)

        if self.peek() is entry:
            entry.get_ready_future()

    def remove_entry(self, index):
        del self.entries[index]

    async def get_next_entry(self, predownload_next=True):
        """
            A coroutine which will return the next song or None if no songs left to play.

            Additionally, if predownload_next is set to True, it will attempt to download the next
            song to be played - so that it's ready by the time we get to it.
        """
        if not self.entries:
            return None

        entry = self.entries.popleft()

        if predownload_next:
            next_entry = self.peek()
            if next_entry:
                next_entry.get_ready_future()

        return await entry.get_ready_future()

    def peek(self):
        """
            Returns the next entry that should be scheduled to be played.
        """
        if self.entries:
            return self.entries[0]

    async def estimate_time_until(self, position, player):
        """
            (very) Roughly estimates the time till the queue will 'position'
        """
        estimated_time = sum(e.duration for e in islice(self.entries, position - 1))

        # When the player plays a song, it eats the first playlist item, so we just have to add the time back
        if not player.is_stopped and player.current_entry:
            estimated_time += player.current_entry.duration - player.progress

        return datetime.timedelta(seconds=estimated_time)

    def count_for_user(self, user):
        return sum(1 for e in self.entries if e.meta.get('author', None) == user)


    def __json__(self):
        return self._enclose_json({
            'entries': list(self.entries)
        })

    @classmethod
    def _deserialize(cls, raw_json, bot=None):
        assert bot is not None, cls._bad('bot')
        # log.debug("Deserializing playlist")
        pl = cls(bot)

        for entry in raw_json['entries']:
            pl.entries.append(entry)

        # TODO: create a function to init downloading (since we don't do it here)?
        return pl

ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'
}

# Fuck your useless bugreports message that gets two link embeds and confuses users
youtube_dl.utils.bug_reports_message = lambda: ''

'''
    Alright, here's the problem.  To catch youtube-dl errors for their useful information, I have to
    catch the exceptions with `ignoreerrors` off.  To not break when ytdl hits a dumb video
    (rental videos, etc), I have to have `ignoreerrors` on.  I can change these whenever, but with async
    that's bad.  So I need multiple ytdl objects.

'''

class Downloader:
    def __init__(self, download_folder=None):
        self.thread_pool = ThreadPoolExecutor(max_workers=2)
        self.unsafe_ytdl = youtube_dl.YoutubeDL(ytdl_format_options)
        self.safe_ytdl = youtube_dl.YoutubeDL(ytdl_format_options)
        self.safe_ytdl.params['ignoreerrors'] = True
        self.download_folder = download_folder

        if download_folder:
            otmpl = self.unsafe_ytdl.params['outtmpl']
            self.unsafe_ytdl.params['outtmpl'] = os.path.join(download_folder, otmpl)
            # print("setting template to " + os.path.join(download_folder, otmpl))

            otmpl = self.safe_ytdl.params['outtmpl']
            self.safe_ytdl.params['outtmpl'] = os.path.join(download_folder, otmpl)


    @property
    def ytdl(self):
        return self.safe_ytdl

    async def extract_info(self, loop, *args, on_error=None, retry_on_error=False, **kwargs):
        """
            Runs ytdl.extract_info within the threadpool. Returns a future that will fire when it's done.
            If `on_error` is passed and an exception is raised, the exception will be caught and passed to
            on_error as an argument.
        """
        if callable(on_error):
            try:
                return await loop.run_in_executor(self.thread_pool, functools.partial(self.unsafe_ytdl.extract_info, *args, **kwargs))

            except Exception as e:

                # (youtube_dl.utils.ExtractorError, youtube_dl.utils.DownloadError)
                # I hope I don't have to deal with ContentTooShortError's
                if asyncio.iscoroutinefunction(on_error):
                    asyncio.ensure_future(on_error(e), loop=loop)

                elif asyncio.iscoroutine(on_error):
                    asyncio.ensure_future(on_error, loop=loop)

                else:
                    loop.call_soon_threadsafe(on_error, e)

                if retry_on_error:
                    return await self.safe_extract_info(loop, *args, **kwargs)
        else:
            return await loop.run_in_executor(self.thread_pool, functools.partial(self.unsafe_ytdl.extract_info, *args, **kwargs))

    async def safe_extract_info(self, loop, *args, **kwargs):
        return await loop.run_in_executor(self.thread_pool, functools.partial(self.safe_ytdl.extract_info, *args, **kwargs))


