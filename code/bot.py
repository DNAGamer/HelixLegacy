allow_requests = True
import asyncio, inspect, json, logging, os, pathlib, random, shlex, shutil, sys, time, traceback, urllib.request, aiohttp, logmein, re
import code.misc
import discord
from requests import get
import async_timeout, socket
from collections import defaultdict
from datetime import timedelta
from functools import wraps
from imp import reload
from io import BytesIO, StringIO
from textwrap import dedent
from urllib.parse import parse_qs
from bs4 import BeautifulSoup
from discord.enums import ChannelType
from discord.ext.commands.bot import _get_variable
from discord.http import _func_
from lxml import etree
from discord import opus
from aiohttp import ClientSession

from . import core
from .core import DISCORD_MSG_CHAR_LIMIT, AUDIO_CACHE_PATH, SkipState, Response, VoiceStateUpdate, StreamPlaylistEntry, Permissions, PermissionsDefaults, Config, ConfigDefaults, load_opus_lib, load_file, write_file, sane_round_int, fixg, ftimedelta
from .core import BOTVERSION
from .player import MusicPlayer, Playlist, Downloader




log = logging.getLogger(__name__)
load_opus_lib()

class Helix(discord.Client):
    def __init__(self, config_file=None, perms_file=None):
        if config_file is None:
            config_file = ConfigDefaults.options_file
        if perms_file is None:
            perms_file = PermissionsDefaults.perms_file
        self.players = {}
        self.exit_signal = None
        self.init_ok = False
        self.cached_app_info = None
        self.last_status = None
        self.playlisturl = "https://gist.githubusercontent.com/DNAGamer/841aa5876ae20b3e52baf5045dc1dfce/raw/c0ced708b63a2b7186863d55953a075472444202/"
        self.config = Config(config_file)
        self.permissions = Permissions(perms_file, grant_all=[self.config.owner_id])
        self.autoplaylist = load_file(self.config.auto_playlist_file)
        self.blacklist = set(load_file(self.config.blacklist_file))
        self.aiolocks = defaultdict(asyncio.Lock)
        self.downloader = Downloader(download_folder='audio_cache')

        self._setup_logging()

        self.rank = "/user"

        if self.blacklist:
            log.debug("Loaded blacklist with {} entries".format(len(self.blacklist)))

        ssd_defaults = {
            'last_np_msg': None,
            'last_np_msg': None,
            'auto_paused': False,
            'availability_paused': False
        }
        self.server_specific_data = defaultdict(ssd_defaults.copy)

        super().__init__()
        self.aiosession = aiohttp.ClientSession(loop=self.loop)
        self.http.user_agent += ' Helix/%s' % BOTVERSION

    def __del__(self):
        # These functions return futures but it doesn't matter
        try:
            self.http.session.close()
        except:
            pass

        try:
            self.aiosession.close()
        except:
            pass

    # TODO: Add some sort of `denied` argument for a message to send when someone else tries to use it
    def owner_only(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            # Only allow the owner to use these commands
            orig_msg = _get_variable('message')

            if not orig_msg or orig_msg.author.id == self.config.owner_id:
                # noinspection PyCallingNonCallable
                return await func(self, *args, **kwargs)
            else:
                raise core.PermissionsError("only the owner can use this command", expire_in=30)

        return wrapper

    def dev_only(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            orig_msg = _get_variable('message')

            if orig_msg.author.id in self.config.dev_ids:
                # noinspection PyCallingNonCallable
                return await func(self, *args, **kwargs)
            else:
                raise core.PermissionsError("only dev users can use this command", expire_in=30)

        wrapper.dev_cmd = True
        return wrapper

    def ensure_appinfo(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            await self._cache_app_info()
            # noinspection PyCallingNonCallable
            return await func(self, *args, **kwargs)

        return wrapper

    def _get_owner(self, *, server=None, voice=False):
        return discord.utils.find(
            lambda m: m.id == self.config.owner_id and (m.voice_channel if voice else True),
            server.members if server else self.get_all_members()
        )

    def _delete_old_audiocache(self, path=AUDIO_CACHE_PATH):
        try:
            shutil.rmtree(path)
            return True
        except:
            try:
                os.rename(path, path + '__')
            except:
                return False
            try:
                shutil.rmtree(path)
            except:
                os.rename(path + '__', path)
                return False

        return True

    def _setup_logging(self):
        if len(logging.getLogger(__package__).handlers) > 1:
            log.debug("Skipping logger setup, already set up")
            return
        import colorlog
        shandler = logging.StreamHandler(stream=sys.stdout)
        shandler.setFormatter(colorlog.LevelFormatter(
            fmt={
                'DEBUG': '{log_color}[{levelname}:] {message}',
                'INFO': '{log_color}{message}',
                'WARNING': '{log_color}{levelname}: {message}',
                'ERROR': '{log_color}[{levelname}:] {message}',
                'CRITICAL': '{log_color}[{levelname}:] {message}',

                'EVERYTHING': '{log_color}[{levelname}:] {message}',
                'NOISY': '{log_color}[{levelname}:] {message}',
                'VOICEDEBUG': '{log_color}[{levelname}:] {message}',
                'FFMPEG': '{log_color}[{levelname}:] {message}'
            },
            log_colors={
                'DEBUG': 'cyan',
                'INFO': 'white',
                'WARNING': 'yellow',
                'ERROR': 'red',
                'CRITICAL': 'bold_red',

                'EVERYTHING': 'white',
                'NOISY': 'white',
                'FFMPEG': 'bold_purple',
                'VOICEDEBUG': 'purple',
            },
            style='{',
            datefmt=''
        ))
        shandler.setLevel(logging.INFO)
        logging.getLogger(__package__).addHandler(shandler)
        logging.getLogger("FFMPEG").setLevel(logging.ERROR)
        logging.getLogger("player").setLevel(logging.ERROR)
        logging.getLogger("discord.gateway ").setLevel(logging.ERROR)

        log.debug("Set logging level to {}".format(self.config.debug_level_str))

    async def rankcheck(self, message):
        try:
            if message.author == self.user or message.author == None or message.author.bot:
                    return
            elif str(message.server.id) in open('level_blck.txt').read():
                return
        except:
            return

        if not os.path.exists("data/" + message.server.id + '/ranking.json'):
            os.mkdir("data/{}".format(message.server.id))
            with open("data/" + message.server.id + '/ranking.json', 'w') as f:
                entry = {message.author.id: {'Rank': 'User', 'XP': "0", 'Level': "1"}}
                json.dump(entry, f)
                f.seek(0)
                f.write(json.dumps(lvldb))
                f.truncate()
                return

        try:
            with open("data/" + message.server.id + '/ranking.json', 'r+') as f:
                lvldb = json.load(f)
                lookup = message.author.id
                if lookup in lvldb:

                    #calculating the ammount of xp to give out
                    if len(message.content) >= 100:
                        lvldb[message.author.id]['XP'] = str(int(lvldb[message.author.id]['XP']) + int((len(message.content) / random.randint(100, 500))))
                    elif len(message.content) <= 13:
                        lvldb[message.author.id]['XP'] = lvldb[message.author.id]['XP']
                    else:
                        lvldb[message.author.id]['XP'] = str(int(lvldb[message.author.id]['XP']) + int((len(message.content)/random.randint(1, 200))))

                    f.seek(0)
                    f.write(json.dumps(lvldb))

                    if (int(lvldb[message.author.id]['XP']) >= int(lvldb[message.author.id]['Level']) * 40):
                        lvldb[message.author.id]['Level'] = str(int(lvldb[message.author.id]['Level']) + 1)
                        lvldb[message.author.id]['XP'] = "0"
                        f.seek(0)
                        f.write(json.dumps(lvldb))
                        f.truncate()

                        lvldb = "" #clearing the json variable
                        f = open("data/" + message.server.id + '/ranking.json', 'r+')
                        lvldb = json.load(f) #reloading it
                        if lvldb[message.author.id]['XP'] == "0": #trying to stop that double message bug
                            level = str(lvldb[message.author.id]['Level'])
                            await self.send_message(message.channel, "Congrats {}, you leveled up!\nYou are now level {}".format(message.author.mention, level))

                else:
                    with open("data/" + message.server.id + '/ranking.json', 'w+') as f:
                        entry = {message.author.id: {'Rank': 'User', 'XP': "0", 'Level': "1"}}
                        lvldb.update(entry)
                        f.write(json.dumps(lvldb))
                f.truncate()
        except Exception as e:
            pass

    async def dsync(self):
        dbotapi = 'https://bots.discord.pw/api'
        url = '{0}/bots/206789942301425664/stats'.format(dbotapi)
        header = {
            'authorization': str(self.discordpw),
            'content-type': 'application/json'
        }
        data = json.dumps({
            "server_count": len(self.servers)
        })
        try:
            log.info("Attempting to update Discord.pw")
            async with aiohttp.post(url, data=data, headers= header) as r:
                print(await r)
                print(await r.text())
        except:
            log.error("Unable to update discord.pw")
            return

        try:
            log.info("Pulling total servers from discord.pw")
            async with aiohttp.get(url=url, header=header) as r:
                text = str(json.loads(await r.json()))
            text = text.split('{')
            try:
                item = text[1]
                item = item.replace("'server_count':", "")
                item = item.replace("}", "")
                item = item.replace(",", "")
                item = item.replace("]", "")
                item = item.replace(" ", "")
                temp = int(item)
                n1 = temp
                item = text[2]
                item = item.replace("'server_count':", "")
                item = item.replace("}", "")
                item = item.replace(",", "")
                item = item.replace("]", "")
                item = item.replace(" ", "")
                temp = int(item)
                n2 = temp

            except Exception as e:
                log.error(e)
                pass
            self.currentcount = n1 + n2
        except:
            log.error("Unable to pull updated server count from discord.pw")
            return

        try:
            payload = {
                "token": str(self.listbots),
                "servers": int(self.currentcount)
            }
            url = "https://bots.discordlist.net/api.php"
            async with aiohttp.post(url, data=payload) as r:
                log.info(await r.text())
        except:
            log.error("Unable to update discordlistbots")


    @staticmethod
    def _check_if_empty(vchannel: discord.Channel, *, excluding_me=True, excluding_deaf=True):
        def check(member):
            if excluding_me and member == vchannel.server.me:
                return False

            if excluding_deaf and any([member.deaf, member.self_deaf]):
                return False

            return True

        return not sum(1 for m in vchannel.voice_members if check(m))

    async def _wait_delete_msg(self, message, after):
        await asyncio.sleep(after)
        await self.safe_delete_message(message, quiet=True)

    async def _manual_delete_check(self, message, *, quiet=False):
        if self.config.delete_invoking:
            await self.safe_delete_message(message, quiet=quiet)

    async def _check_ignore_non_voice(self, msg):
        vc = msg.server.me.voice_channel
        if not vc or vc == msg.author.voice_channel:
            return True
        else:
            raise core.PermissionsError(
                "you cannot use this command when not in the voice channel (%s)" % vc.name, expire_in=30)

    async def _cache_app_info(self, *, update=False):
        if not self.cached_app_info and not update and self.user.bot:
            log.debug("Caching app info")
            self.cached_app_info = await self.application_info()

        return self.cached_app_info

    async def get_voice_client(self, channel: discord.Channel):
        if isinstance(channel, discord.Object):
            channel = self.get_channel(channel.id)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('Channel passed must be a voice channel')

        async with self.aiolocks[_func_() + ':' + channel.server.id]:
            if self.is_voice_connected(channel.server):
                return self.voice_client_in(channel.server)

            vc = None
            t0 = t1 = 0
            tries = 5

            for attempt in range(1, tries + 1):
                log.debug("Connection attempt {} to {}".format(attempt, channel.name))
                t0 = time.time()

                try:
                    vc = await self.join_voice_channel(channel)
                    t1 = time.time()
                    break

                except asyncio.TimeoutError:
                    log.warning("Failed to connect, retrying ({}/{})".format(attempt, tries))

                    # TODO: figure out if I need this or not
                    # try:
                    #     await self.ws.voice_state(channel.server.id, None)
                    # except:
                    #     pass

                except:
                    log.exception("Unknown error attempting to connect to voice")

                await asyncio.sleep(0.5)

            if not vc:
                log.critical("Voice client is unable to connect, restarting...")
                await self.restart()

            log.debug("Connected in {:0.1f}s".format(t1 - t0))
            log.info("Connected to {}/{}".format(channel.server, channel))

            vc.ws._keep_alive.name = 'VoiceClient Keepalive'

            return vc

    async def reconnect_voice_client(self, server, *, sleep=0.1, channel=None):
        log.debug("Reconnecting voice client on \"{}\"{}".format(
            server, ' to "{}"'.format(channel.name) if channel else ''))

        async with self.aiolocks[_func_() + ':' + server.id]:
            vc = self.voice_client_in(server)

            if not (vc or channel):
                return

            _paused = False
            player = self.get_player_in(server)

            if player and player.is_playing:
                log.voicedebug("(%s) Pausing", _func_())

                player.pause()
                _paused = True

            log.voicedebug("(%s) Disconnecting", _func_())

            try:
                await vc.disconnect()
            except:
                pass

            if sleep:
                log.voicedebug("(%s) Sleeping for %s", _func_(), sleep)
                await asyncio.sleep(sleep)

            if player:
                log.voicedebug("(%s) Getting voice client", _func_())

                if not channel:
                    new_vc = await self.get_voice_client(vc.channel)
                else:
                    new_vc = await self.get_voice_client(channel)

                log.voicedebug("(%s) Swapping voice client", _func_())
                await player.reload_voice(new_vc)

                if player.is_paused and _paused:
                    log.voicedebug("Resuming")
                    player.resume()

        log.debug("Reconnected voice client on \"{}\"{}".format(
            server, ' to "{}"'.format(channel.name) if channel else ''))

    async def disconnect_voice_client(self, server):
        vc = self.voice_client_in(server)
        if not vc:
            return

        if server.id in self.players:
            self.players.pop(server.id).kill()

        await vc.disconnect()

    async def disconnect_all_voice_clients(self):
        for vc in list(self.voice_clients).copy():
            await self.disconnect_voice_client(vc.channel.server)

    async def set_voice_state(self, vchannel, *, mute=False, deaf=False):
        if isinstance(vchannel, discord.Object):
            vchannel = self.get_channel(vchannel.id)

        if getattr(vchannel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('Channel passed must be a voice channel')

        await self.ws.voice_state(vchannel.server.id, vchannel.id, mute, deaf)
        # I hope I don't have to set the channel here
        # instead of waiting for the event to update it

    def get_player_in(self, server: discord.Server) -> MusicPlayer:
        return self.players.get(server.id)

    async def fetch(self, session, url):
        with async_timeout.timeout(3):
            async with session.get(url) as response:
                return await response.text()

    async def get_player(self, channel, create=False, *, deserialize=False) -> MusicPlayer:
        server = channel.server
        xcx = channel
        async with self.aiolocks[_func_() + ':' + server.id]:
            if deserialize:
                voice_client = await self.get_voice_client(channel)
                player = await self.deserialize_queue(server, voice_client)

                if player:
                    log.debug("Created player via deserialization for server %s with %s entries", server.id,
                              len(player.playlist))
                    # Since deserializing only happens when the bot starts, I should never need to reconnect
                    return self._init_player(player, server=server)
            dir = "data/settings/" + server.id + ".json"
            if not os.path.exists("data"):
                os.mkdir("data")
            if not os.path.exists("data/settings"):
                os.mkdir("data/settings")
            if not os.path.isfile(dir):
                prefix = self.config.command_prefix
            else:
                with open(dir, 'r') as r:
                    data = json.load(r)
                    prefix = str(data["prefix"])
            if server.id not in self.players:
                if not create:
                    try:
                        await self.send_message(xcx, 'Im not in a voice channel\nUse %sspawn to spawn me to your voice channel.' % prefix)
                        return
                    except:
                        pass

                voice_client = await self.get_voice_client(channel)

                playlist = Playlist(self)
                player = MusicPlayer(self, voice_client, playlist)
                self._init_player(player, server=server)

            async with self.aiolocks[self.reconnect_voice_client.__name__ + ':' + server.id]:
                if self.players[server.id].voice_client not in self.voice_clients:
                    log.debug("Reconnect required for voice client in {}".format(server.name))
                    await self.reconnect_voice_client(server, channel=channel)

        return self.players[server.id]

    def _init_player(self, player, *, server=None):
        player = player.on('play', self.on_player_play) \
            .on('resume', self.on_player_resume) \
            .on('pause', self.on_player_pause) \
            .on('stop', self.on_player_stop) \
            .on('finished-playing', self.on_player_finished_playing) \
            .on('entry-added', self.on_player_entry_added) \
            .on('error', self.on_player_error)

        player.skip_state = SkipState()

        if server:
            self.players[server.id] = player

        return player

    async def on_player_play(self, player, entry):
        await self.update_now_playing_status(entry)
        player.skip_state.reset()

        # This is the one event where its ok to serialize autoplaylist entries
        await self.serialize_queue(player.voice_client.channel.server)

        channel = player.voice_client.channel
        channel = self.server_specific_data[channel.server]['last_np_msg']
        channel = channel.channel
        author = entry.meta.get('author', None)

        if channel and author:
            last_np_msg = self.server_specific_data[channel.server]['last_np_msg']
            if last_np_msg and last_np_msg.channel == channel:

                async for lmsg in self.logs_from(channel, limit=1):
                    if lmsg != last_np_msg and last_np_msg:
                        self.server_specific_data[channel.server]['last_np_msg'] = None
                    break  # This is probably redundant

            newmsg = 'Now playing in %s: **%s**' % (
                player.voice_client.channel.name, entry.title)
            thumbnail = entry.url
            thumbnail = thumbnail.replace("www.", "")
            thumbnail = thumbnail.replace("https://youtube.com/watch?v=", "http://img.youtube.com/vi/")
            thumbnail = thumbnail + "/mqdefault.jpg"
            em = discord.Embed(description=newmsg, colour=(random.randint(0, 16777215)))
            em.set_image(url=thumbnail)
            if self.server_specific_data[channel.server]['last_np_msg']:
                    self.server_specific_data[channel.server]['last_np_msg'] = await self.send_message(channel,embed=em)
            else:
                self.server_specific_data[channel.server]['last_np_msg'] = await self.send_message(channel, embed=em)

    async def on_player_resume(self, player, entry, **_):
        await self.update_now_playing_status(entry)

    async def on_player_pause(self, player, entry, **_):
        await self.update_now_playing_status(entry, True)
        await self.serialize_queue(player.voice_client.channel.server)

    async def on_player_stop(self, player, **_):
        await self.update_now_playing_status()

    async def on_player_finished_playing(self, player, **_):
        await self.serialize_queue(player.voice_client.channel.server)
        if not player.playlist.entries:
            if player.is_playing:
                return
            else:
                pass
        else:
            return
        while self.autoplaylist:
            random.shuffle(self.autoplaylist)
            song_url = random.choice(self.autoplaylist)

            info = {}

            try:
                info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
            except self.downloader.youtube_dl.utils.DownloadError as e:
                if 'YouTube said:' in e.args[0]:
                    # url is bork, remove from list and put in removed list
                    log.error("Error processing youtube url:\n{}".format(e.args[0]))

                else:
                    # Probably an error from a different extractor, but I've only seen youtube's
                    log.error("Error processing \"{url}\": {ex}".format(url=song_url, ex=e))

                await self.remove_from_autoplaylist(song_url, ex=e, delete_from_ap=True)
                continue

            except Exception as e:
                log.error("Error processing \"{url}\": {ex}".format(url=song_url, ex=e))
                log.exception()

                self.autoplaylist.remove(song_url)
                continue

            if info.get('entries', None):  # or .get('_type', '') == 'playlist'
                log.debug("Playlist found but is unsupported at this time, skipping.")
                # TODO: Playlist expansion

            # Do I check the initial conditions again?
            # not (not player.playlist.entries and not player.current_entry and self.config.auto_playlist)

            try:
                await player.playlist.add_entry(song_url, channel=None, author=None)
            except Exception as e:
                log.error("Error adding song from autoplaylist: {}".format(e))
                log.debug('', exc_info=True)
                continue
            break

    async def on_player_entry_added(self, player, playlist, entry, **_):
        if entry.meta.get('author') and entry.meta.get('channel'):
            await self.serialize_queue(player.voice_client.channel.server)

    async def on_player_error(self, player, entry, ex, **_):
        if 'channel' in entry.meta:
            await self.safe_send_message(
                entry.meta['channel'],
                "```\nError from FFmpeg:\n{}\n```".format(ex)
            )
        else:
            log.exception("Player error", exc_info=ex)

    async def update_now_playing_status(self, entry=None, is_paused=False):
        game = None
        game = """music somewhere
with code
something, idk
some really messed up stuff
with /help
with commands
porn
VIDEO GAMES
Overwatch
MLG Pro Simulator
stuff
with too many servers
with life of my dev
dicks
Civ 5
Civ 6
Besiege
with code
Mass Effect
bangin tunes
with children
with jews
on a new server
^-^
with something
the violin
For cuddles
the harmonica
With dicks
With a gas chamber
Nazi simulator 2K17
Rodina
Gas bills
Memes
Darkness
With some burnt toast
Jepus Crist
With my devs nipples
SOMeBODY ONCE TOLD ME
With Hitler's dick
In The Street
With Knives
ɐᴉlɐɹʇsn∀ uI
Shrek Is Love
Shrek Is Life
Illegal Poker
ACROSS THE UNIVERRRRSE
Kickball
Mah Jam
R2-D2 On TV
with fire
at being a real bot
with your fragile little mind"""
        text = game.splitlines()
        size = len(text)
        try:
            null = text[random.randint(0, size)]
            null = str(null)
        except IndexError:
            null = text[1]
        if null == None or null == "":
            null = text[2]
        name = str(null)
        await self.change_presence(game=discord.Game(name=name))

    async def update_now_playing_message(self, server, message, *, channel=None):
        lnp = self.server_specific_data[server]['last_np_msg']
        m = None

        em = discord.Embed(description=message, colour=(random.randint(0, 16777215)))
        em.set_author(name='Song:', icon_url="http://images.clipartpanda.com/help-clipart-11971487051948962354zeratul_Help.svg.med.png")
        await self.send_message(channel, embed=em)

        if message is None and lnp:
            await self.safe_delete_message(lnp, quiet=True)

        elif lnp:  # If there was a previous lp message
            oldchannel = lnp.channel

            if lnp.channel == oldchannel:  # If we have a channel to update it in
                async for lmsg in self.logs_from(channel, limit=1):
                    if lmsg != lnp and lnp:  # If we need to resend it
                        await self.safe_delete_message(lnp, quiet=True)
                        m = await self.send_message(channel, embed=em)
                    else:
                        m = await self.safe_edit_message(lnp, message, send_if_fail=True, quiet=False)

            elif channel:  # If we have a new channel to send it to
                await self.safe_delete_message(lnp, quiet=True)
                m = await self.send_message(channel, embed=em)

            else:  # we just resend it in the old channel
                await self.safe_delete_message(lnp, quiet=True)
                m = await self.send_message(oldchannel, embed=em)

        elif channel:  # No previous message
            m = await self.send_message(channel, embed=em)

        self.server_specific_data[server]['last_np_msg'] = m

    async def serialize_queue(self, server, *, dir=None):
        """
        Serialize the current queue for a server's player to json.
        """

        player = self.get_player_in(server)
        if not player:
            return

        if dir is None:
            dir = 'user/%s/queue.json' % server.id

        async with self.aiolocks['queue_serialization' + ':' + server.id]:
            log.debug("Serializing queue for %s", server.id)

            with open(dir, 'w', encoding='utf8') as f:
                f.write(player.serialize(sort_keys=True))

    async def serialize_all_queues(self, *, dir=None):
        coros = [self.serialize_queue(s, dir=dir) for s in self.servers]
        await asyncio.gather(*coros, return_exceptions=True)

    async def deserialize_queue(self, server, voice_client, playlist=None, *, dir=None) -> MusicPlayer:
        """
        Deserialize a saved queue for a server into a MusicPlayer.  If no queue is saved, returns None.
        """

        if playlist is None:
            playlist = Playlist(self)

        if dir is None:
            dir = 'user/%s/queue.json' % server.id

        async with self.aiolocks['queue_serialization' + ':' + server.id]:
            if not os.path.isfile(dir):
                return None

            log.debug("Deserializing queue for %s", server.id)

            with open(dir, 'r', encoding='utf8') as f:
                data = f.read()

        return MusicPlayer.from_json(data, self, voice_client, playlist)

    async def _on_ready_sanity_checks(self):
        # Ensure folders exist
        await self._scheck_ensure_env()

        # Server permissions check
        await self._scheck_server_permissions()

        # config/permissions async validate?
        await self._scheck_configs()

    async def _scheck_ensure_env(self):
        log.debug("Ensuring data folders exist")
        for server in self.servers:
            pathlib.Path('user/%s/' % server.id).mkdir(exist_ok=True)

        with open('user/server_names.txt', 'w', encoding='utf8') as f:
            for server in sorted(self.servers, key=lambda s: int(s.id)):
                f.write('{:<22} {}\n'.format(server.id, server.name))

        if not self.config.save_videos and os.path.isdir(AUDIO_CACHE_PATH):
            if self._delete_old_audiocache():
                log.debug("Deleted old audio cache")
            else:
                log.debug("Could not delete old audio cache, moving on.")

    async def _scheck_ensure_env(self):
        log.debug("Ensuring data folders exist")
        for server in self.servers:
            pathlib.Path('user/%s/' % server.id).mkdir(exist_ok=True)

        with open('user/server_names.txt', 'w', encoding='utf8') as f:
            for server in sorted(self.servers, key=lambda s: int(s.id)):
                f.write('{:<22} {}\n'.format(server.id, server.name))

        if not self.config.save_videos and os.path.isdir(AUDIO_CACHE_PATH):
            if self._delete_old_audiocache():
                log.debug("Deleted old audio cache")
            else:
                log.debug("Could not delete old audio cache, moving on.")

    async def _scheck_server_permissions(self):
        log.debug("Checking server permissions")
        pass  # TODO

    async def _scheck_configs(self):
        log.debug("Validating config")
        await self.config.async_validate(self)

        log.debug("Validating permissions config")
        await self.permissions.async_validate(self)

    #######################################################################################################################



    async def safe_send_message(self, dest, content, *, tts=False, expire_in=0, also_delete=None, quiet=False):
        msg = None
        try:
            msg = await self.send_message(dest, content)
        except discord.Forbidden:
            if not quiet:
                await self.safe_send_message((discord.Object(id='228835542417014784')),
                                             "Warning: Cannot send message to %s, no permission" % dest.name)
        except discord.NotFound:
            if not quiet:
                await self.safe_send_message((discord.Object(id='228835542417014784')),
                                             "Warning: Cannot send message to %s, invalid channel?" % dest.name)
        return msg

    async def safe_delete_message(self, message, *, quiet=False):
        lfunc = log.debug if quiet else log.warning

        try:
            if "/toast" in str(message):
                return
            return await self.delete_message(message)

        except discord.Forbidden:
            lfunc("Cannot delete message \"{}\", no permission".format(message.clean_content))

        except discord.NotFound:
            lfunc("Cannot delete message \"{}\", message not found".format(message.clean_content))

    async def safe_edit_message(self, message, new, *, send_if_fail=False, quiet=False):
        lfunc = log.debug if quiet else log.warning

        try:
            return await self.edit_message(message, new)

        except discord.NotFound:
            lfunc("Cannot edit message \"{}\", message not found".format(message.clean_content))
            if send_if_fail:
                lfunc("Sending message instead")
                return await self.safe_send_message(message.channel, new)

    async def send_typing(self, destination):
        try:
            return await super().send_typing(destination)
        except discord.Forbidden:
            log.warning("Could not send typing to {}, no permission".format(destination))

    async def edit_profile(self, **fields):
        if self.user.bot:
            return await super().edit_profile(**fields)
        else:
            return await super().edit_profile(self.config._password, **fields)

    async def restart(self):
        self.exit_signal = core.RestartSignal()
        await self.logout()

    def restart_threadsafe(self):
        asyncio.run_coroutine_threadsafe(self.restart(), self.loop)

    def _cleanup(self):
        try:
            self.loop.run_until_complete(self.logout())
        except:
            pass

        pending = asyncio.Task.all_tasks()
        gathered = asyncio.gather(*pending)

        try:
            gathered.cancel()
            self.loop.run_until_complete(gathered)
            gathered.exception()
        except:
            pass

    def run(self):
        try:
            self.loop.run_until_complete(self.start(logmein.token()))

        except discord.errors.LoginFailure:
            log.error("token invalid")
            code = """def token():
                            token = "[put your token here]"
                            return token"""
            time.sleep(3)
            token = input("please input a bot token: ")
            token = token.replace("\n", "")
            token = token.replace(" ", "")
            code = code.replace('[put your token here]', token)
            target = open("logmein.py", "w")
            target.writelines(code)
            target.close()
            os.execl(sys.executable, sys.executable, *sys.argv)
        except KeyboardInterrupt:
            self.loop.run_until_complete(self.logout())
            pending = asyncio.Task.all_tasks(loop=self.loop)
            gathered = asyncio.gather(*pending, loop=self.loop)
            gathered.exception()
        finally:
            try:
                self._cleanup()
            except Exception:
                log.error("Error in cleanup", exc_info=True)

            self.loop.close()
            if self.exit_signal:
                raise self.exit_signal

    async def logout(self):
        await self.disconnect_all_voice_clients()
        return await super().logout()

    async def on_error(self, event, *args, **kwargs):
        ex_type, ex, stack = sys.exc_info()

        if ex_type == core.HelpfulError:
            log.error("Exception in {}:\n{}".format(event, ex.message))

            await asyncio.sleep(2)  # don't ask
            await self.logout()

        elif issubclass(ex_type, core.Signal):
            self.exit_signal = ex_type
            await self.logout()

        else:
            log.error("Exception in {}".format(event), exc_info=True)

    async def on_resumed(self):
        log.info("\nReconnected to discord.\n")

    async def on_ready(self):
        log.debug("Connection established, ready to go.")

        self.ws._keep_alive.name = 'Gateway Keepalive'

        if self.init_ok:
            log.debug("Received additional READY event, may have failed to resume")
            return

        await self._on_ready_sanity_checks()
        print()

        log.info('Connected!  Helix v{}\n'.format(BOTVERSION))

        self.init_ok = True

        ################################

        log.info("Bot:   {0}/{1}#{2}{3}".format(
            self.user.id,
            self.user.name,
            self.user.discriminator,
            ' [BOT]' if self.user.bot else ' [Userbot]'
        ))

        owner = self._get_owner(voice=True) or self._get_owner()
        if owner and self.servers:
            log.info("Owner: {0}/{1}#{2}\n".format(
                owner.id,
                owner.name,
                owner.discriminator
            ))

            log.info('Server List:')
            [log.info(' - ' + s.name) for s in self.servers]

        elif self.servers:
            log.warning("Owner could not be found on any server (id: %s)\n" % self.config.owner_id)

            log.info('Server List:')
            [log.info(' - ' + s.name) for s in self.servers]

        else:
            log.warning("Owner unknown, bot is not on any servers.")
            if self.user.bot:
                log.warning(
                    "To make the bot join a server, paste this link in your browser. \n"
                    "Note: You should be logged into your main account and have \n"
                    "manage server permissions on the server you want the bot to join.\n"
                    "  " + await self.generate_invite_link()
                )

        print(flush=True)

        print(flush=True)
        log.info("Options:")

        log.info("  Command prefix: " + self.config.command_prefix)
        log.info("  Default volume: {}%".format(int(self.config.default_volume * 100)))
        log.info("  Skip threshold: {} votes or {}%".format(
            self.config.skips_required, fixg(self.config.skip_ratio_required * 100)))
        log.info("  Now Playing @mentions: " + ['Disabled', 'Enabled'][self.config.now_playing_mentions])
        log.info("  Auto-Pause: " + ['Disabled', 'Enabled'][self.config.auto_pause])
        log.info("  Delete Messages: " + ['Disabled', 'Enabled'][self.config.delete_messages])
        if self.config.delete_messages:
            log.info("    Delete Invoking: " + ['Disabled', 'Enabled'][self.config.delete_invoking])
        log.info("  Debug Mode: " + ['Disabled', 'Enabled'][self.config.debug_mode])
        log.info("  Downloaded songs will be " + ['deleted', 'saved'][self.config.save_videos])
        print(flush=True)
        #self.dsync()
        await self.change_presence(game=discord.Game(name="Ready"))

    async def get_google_entries(self, query):
        params = {
            'q': query,
            'safe': 'on'
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 6.3; Win64; x64)'
        }

        # list of URLs
        entries = []

        async with aiohttp.get('https://www.google.co.uk/search', params=params, headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError('Google somehow failed to respond.')

            root = etree.fromstring(await resp.text(), etree.HTMLParser())

            """
            Tree looks like this.. sort of..
            <div class="g">
                ...
                <h3>
                    <a href="/url?q=<url>" ...>title</a>
                </h3>
                ...
                <span class="st">
                    <span class="f">date here</span>
                    summary here, can contain <em>tag</em>
                </span>
            </div>
            """

            search_nodes = root.findall(".//div[@class='g']")
            for node in search_nodes:
                url_node = node.find('.//h3/a')
                if url_node is None:
                    continue

                url = url_node.attrib['href']
                if not url.startswith('/url?'):
                    continue

                url = parse_qs(url[5:])['q'][0]  # get the URL from ?q query string

                # if I ever cared about the description, this is how
                entries.append(url)

                # short = node.find(".//span[@class='st']")
                # if short is None:
                #     entries.append((url, ''))
                # else:
                #     text = ''.join(short.itertext())
                #     entries.append((url, text.replace('...', '')))

        return entries

#
#
#
#utility stuff

    async def cmd_help(self, author, channel, server):
        """
        Usage:
            {command_prefix}help
        a help message.
        """
        await self.send_typing(channel)

        dir = "data/settings/" + server.id + ".json"
        if not os.path.exists("data"):
            os.mkdir("data")
        if not os.path.exists("data/settings"):
            os.mkdir("data/settings")
        if not os.path.isfile(dir):
            prefix = self.config.command_prefix
        else:
            with open(dir, 'r') as r:
                data = json.load(r)
                prefix = str(data["prefix"])

        c = channel
        admins = False
        adults = False
        perms = author.permissions_in(channel)

        for role in author.roles:
            try:
                if perms.administrator or perms.manage_server or perms.manage_channels or perms.manage_messages:
                    admins = True
            except:
                admins = True

        music = str(code.misc.helpmusic())
        utility = str(code.misc.helputility())
        admin = str(code.misc.helpadmin())
        chat = str(code.misc.helpchat())
        adult = str(code.misc.helpadult())

        try:
            music = music.replace("/", prefix)
            utility = utility.replace("/", prefix)
            admin = admin.replace("/", prefix)
            chat = chat.replace("/", prefix)
            adult = adult.replace("/", prefix)
        except Exception as e:
            log.error(e)
            pass
        cname = ""
        sname = ""
        for channel in server.channels:
            name = channel.name
            name = name.lower()
            if "nsfw" in name:
                cname = name
        sname = server.name
        sname = sname.lower()
        if "nsfw" in cname or "nsfw" in sname:
            adults = True

        em = discord.Embed(description="I've sent my commands to you ^-^", colour=(random.randint(0, 16777215)))
        em.set_author(name='Help:', icon_url=self.user.avatar_url)
        try:
            await self.send_message(c, embed=em)
        except:
            await self.safe_send_message(c, "I've sent my commands to you ^-^")

        await self.safe_send_message(author, "**Commands**\n")
        await self.safe_send_message(author, music)
        await self.safe_send_message(author, chat)
        await self.safe_send_message(author, utility)
        if adults:
            await self.safe_send_message(author, adult)
        if admins:
            await self.safe_send_message(author, admin)

    async def cmd_blacklist(self, user_mentions, option, author, channel, server):
        if server.id == "206794668736774155":  # Helixs server
            print("Server is correct")
            perms = author.permissions_in(channel)
            for role in author.roles:
                try:
                    if perms.administrator or perms.manage_server or perms.manage_channels:  # Devs, Tech support
                        usage = True
                    else:
                        return Response("You cant use this command")
                except:
                    await self.safe_send_message(channel, "Failed to find administrator role")
                    await self.safe_send_message(channel, perms)
        else:
            if author.id == "174918559539920897":
                pass
            else:
                return Response("You cant use this command")
        """
        Usage:
            {command_prefix}blacklist [ + | - | add | remove ] @UserName [@UserName2 ...]
        Add or remove users to the blacklist.
        Blacklisted users are forbidden from using bot commands.
        """

        if not user_mentions:
            raise core.CommandError("No users listed.", expire_in=20)

        if option not in ['+', '-', 'add', 'remove']:
            raise core.CommandError(
                'Invalid option "%s" specified, use +, -, add, or remove' % option, expire_in=20
            )

        for user in user_mentions.copy():
            if user.id == self.config.owner_id:
                print("[Commands:Blacklist] The owner cannot be blacklisted.")
                user_mentions.remove(user)

        old_len = len(self.blacklist)

        if option in ['+', 'add']:
            self.blacklist.update(user.id for user in user_mentions)

            write_file(self.config.blacklist_file, self.blacklist)

            return Response(
                '%s users have been added to the blacklist' % (len(self.blacklist) - old_len),
                reply=True, delete_after=10
            )

        else:
            if self.blacklist.isdisjoint(user.id for user in user_mentions):
                return Response('none of those users are in the blacklist.', reply=True, delete_after=10)

            else:
                self.blacklist.difference_update(user.id for user in user_mentions)
                write_file(self.config.blacklist_file, self.blacklist)

                return Response(
                    '%s users have been removed from the blacklist' % (old_len - len(self.blacklist)),
                    reply=True, delete_after=10
                )

    async def cmd_id(self, author, user_mentions):
        """
        Usage:
            {command_prefix}id [@user]
        Tells the user their id or the id of another user.
        """
        if not user_mentions:
            return Response('your id is `%s`' % author.id, reply=True, delete_after=35)
        else:
            usr = user_mentions[0]
            return Response("%s's id is `%s`" % (usr.name, usr.id), reply=True, delete_after=35)

    async def cmd_whois(self, author, user_mentions, channel):
        def userinf(user, channel):
            msg = "Information on " + str(user.mention)
            roles = ""
            for role in user.roles:
                if str(role) == "@everyone":
                    pass
                else:
                    roles += str(role) + "\n"
            em = discord.Embed(description=msg, colour=(random.randint(0, 16777215)))
            em.set_author(name=user.display_name, icon_url=(user.avatar_url))
            em.add_field(name = "**User name:**", value = str(user.name) , inline = True)
            if not author.name == author.display_name: #so we dont have useless fields sat around
                em.add_field(name = "**Nickname:**", value = (str(user.display_name)), inline = True)
            em.add_field(name = "**Created on:**", value = str(user.created_at), inline = True)
            em.add_field(name = "**ID:**", value = str(user.id), inline = True)
            if not roles == "" or roles == " ":
                em.add_field(name = "**Roles**", value = roles, inline = True)
            if user.bot:
                em.add_field(name = "**Bot**", value = "This user is a bot", inline = False)
            em.set_thumbnail(url = user.avatar_url)
            return em

        if not user_mentions:
            user = author #allows us to only use this one script, much cleaner and more efficent
            em =userinf(user, channel)
            try:
                await self.send_message(channel, embed=em)
            except:
                await self.send_message(channel, "You've disabled my permission to 'embed links'")
        else:
            for user in user_mentions:
                em = userinf(user, channel)
                try:
                    await self.send_message(channel, embed=em)
                except:
                    await self.send_message(channel, "You've disabled my permission to 'embed links'")

    async def cmd_clean(self, message, channel, server, author, search_range=5000):
        """
        Usage:
            {command_prefix}clean [range]
        Removes up to [range] messages the bot has posted in chat. Default: 50, Max: 1000
        """
        dir = "data/settings/" + message.server.id + ".json"
        if not os.path.exists("data"):
            os.mkdir("data")
        if not os.path.exists("data/settings"):
            os.mkdir("data/settings")
        if not os.path.isfile(dir):
            prefix = self.config.command_prefix
        else:
            with open(dir, 'r') as r:
                data = json.load(r)
                prefix = str(data["prefix"])
        try:
            float(search_range)  # lazy check
            search_range = min(int(search_range), 1000)
        except:
            return Response("enter a number.  NUMBER.  That means digits.  `15`.  Etc.", reply=True,
                            delete_after=8)

        await self.safe_delete_message(message, quiet=True)

        def is_possible_command_invoke(entry, prefix):
            if entry.content.startswith(prefix):
                return True
            else:
                return False

        delete_invokes = True
        delete_all = channel.permissions_for(
            author).manage_messages or self.config.owner_id == author.id

        def check(message):
            if is_possible_command_invoke(message, prefix) and delete_invokes:
                return delete_all or message.author == author
            return message.author == self.user

        if self.user.bot:
            if channel.permissions_for(server.me).manage_messages:
                deleted = await self.purge_from(channel, check=check, limit=search_range,
                                                before=message)
                return Response('Cleaned up {} message{}.'.format(len(deleted), 's' * bool(deleted)),
                                delete_after=15)

        deleted = 0
        async for entry in self.logs_from(channel, search_range, before=message):
            if entry == self.server_specific_data[channel.server]['last_np_msg']:
                continue

            if entry.author == self.user:
                await self.safe_delete_message(entry)
                deleted += 1
                await asyncio.sleep(0.21)

            if is_possible_command_invoke(entry) and delete_invokes:
                if delete_all or entry.author == author:
                    try:
                        await self.delete_message(entry)
                        await asyncio.sleep(0.21)
                        deleted += 1

                    except discord.Forbidden:
                        delete_invokes = False
                    except discord.HTTPException:
                        pass

        return Response('Cleaned up {} message{}.'.format(deleted, 's' * bool(deleted)), delete_after=6)

                        #

    async def cmd_prefix(self, leftover_args, server, channel, author):
        perms = author.permissions_in(channel)
        rolez = False
        for role in author.roles:
            try:
                if perms.administrator or perms.manage_server:
                    rolez = True
            except:
                await self.safe_send_message(channel, "Failed to find administrator or manage server role")
                await self.safe_send_message(channel, perms)
        if not rolez:
            return
        if leftover_args[0] != "" or leftover_args[0] != " " or leftover_args[0] != None:
            dir = "data/settings/" + server.id + ".json"

            if not os.path.exists("data"):
                os.mkdir("data")
            if not os.path.exists("data/settings"):
                os.mkdir("data/settings")
            if not os.path.isfile(dir):
                with open(dir, 'w') as r:
                    entry = {'prefix': str(leftover_args[0])}
                    json.dump(entry, r)
            else:
                with open(dir, 'r+') as r:
                    data = json.load(r)
                    data['prefix'] = str(leftover_args[0])
                    r.seek(0)
                    r.write(json.dumps(data))
                    r.truncate()

            await self.send_message(channel, "Set prefix to ``" + str(leftover_args[0]) + "``")

    async def cmd_dump(self, server, player, channel, author):

        string = ""
        for entries in player.playlist:
            string += str(entries.url) + "\n"
        name = server.name + " playlist for " + author.name

        GITHUB_API = "https://api.github.com"
        API_TOKEN = 'df989710d449c4f8bd306db2ec7223458c7ce5e1'
        url = GITHUB_API + "/gists"
        headers = {'Authorization': 'token %s' % API_TOKEN}
        params = {'scope': 'gist'}
        payload = {"description": name, "public": True, "files": {"Playlist": {"content": string}}}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, params=params, data=json.dumps(payload)) as resp:
                j = json.loads(await resp.text())

                url = j['url']
                url = url.replace('https://api.github.com/gists/', "https://gist.github.com/DNAGamer/")
                url += "/raw"
                return Response(url)
#
#
#Chat stuff

#
#
#
#Music stuff

    @owner_only
    async def cmd_setname(self, leftover_args, name):
        """
        Usage:
            {command_prefix}setname name
        Changes the bot's username.
        Note: This operation is limited by discord to twice per hour.
        """

        name = ' '.join([name, *leftover_args])

        try:
            await self.edit_profile(username=name)

        except discord.HTTPException:
            raise core.CommandError(
                "Failed to change name.  Did you change names too many times?  "
                "Remember name changes are limited to twice per hour.")

        except Exception as e:
            raise core.CommandError(e, expire_in=20)

        return Response("\N{OK HAND SIGN}", delete_after=20)

    @owner_only
    async def cmd_setavatar(self, message, url=None):
        """
        Usage:
            {command_prefix}setavatar [url]
        Changes the bot's avatar.
        Attaching a file and leaving the url parameter blank also works.
        """

        if message.attachments:
            thing = message.attachments[0]['url']
        else:
            thing = url.strip('<>')

        try:
            with aiohttp.Timeout(10):
                async with self.aiosession.get(thing) as res:
                    await self.edit_profile(avatar=await res.read())

        except Exception as e:
            raise core.CommandError("Unable to change avatar: {}".format(e), expire_in=20)

        return Response("\N{OK HAND SIGN}", delete_after=20)

    @owner_only
    async def cmd_debug(self, author, _player, *, data):
        if author == "174918559539920897":
            pass
        else:
            return Response("no")
        player = _player
        codeblock = "```py\n{}\n```"
        result = None

        if data.startswith('```') and data.endswith('```'):
            data = '\n'.join(data.rstrip('`\n').split('\n')[1:])

        code = data.strip('` \n')

        try:
            result = eval(code)
        except:
            try:
                exec(code)
            except Exception as e:
                traceback.print_exc(chain=False)
                return Response("{}: {}".format(type(e).__name__, e))

        if asyncio.iscoroutine(result):
            result = await result

        return Response(codeblock.format(result))

    async def cmd_setnick(self, server, channel, leftover_args, nick):
        """
        Usage:
            {command_prefix}setnick nick
        Changes the bot's nickname.
        """

        if author.id != "174918559539920897":
            if author != server.owner:
                return

        nick = ' '.join([nick, *leftover_args])

        try:
            await self.change_nickname(server.me, nick)
        except Exception as e:
            raise core.CommandError(e, expire_in=20)

        return Response("\N{OK HAND SIGN}", delete_after=20)

    async def cmd_listids(self, server, author, leftover_args, cat='all'):
        """
        Usage:
            {command_prefix}listids [categories]
        Lists the ids for various things.  Categories are:
           all, users, roles, channels
        """

        cats = ['channels', 'roles', 'users']

        if cat not in cats and cat != 'all':
            return Response(
                "Valid categories: " + ' '.join(['`%s`' % c for c in cats]),
                reply=True,
                delete_after=25
            )

        if cat == 'all':
            requested_cats = cats
        else:
            requested_cats = [cat] + [c.strip(',') for c in leftover_args]

        data = ['Your ID: %s' % author.id]

        for cur_cat in requested_cats:
            rawudata = None

            if cur_cat == 'users':
                data.append("\nUser IDs:")
                rawudata = ['%s #%s: %s' % (m.name, m.discriminator, m.id) for m in server.members]

            elif cur_cat == 'roles':
                data.append("\nRole IDs:")
                rawudata = ['%s: %s' % (r.name, r.id) for r in server.roles]

            elif cur_cat == 'channels':
                data.append("\nText Channel IDs:")
                tchans = [c for c in server.channels if c.type == discord.ChannelType.text]
                rawudata = ['%s: %s' % (c.name, c.id) for c in tchans]

                rawudata.append("\nVoice Channel IDs:")
                vchans = [c for c in server.channels if c.type == discord.ChannelType.voice]
                rawudata.extend('%s: %s' % (c.name, c.id) for c in vchans)

            if rawudata:
                data.extend(rawudata)

        with BytesIO() as sdata:
            sdata.writelines(d.encode('utf8') + b'\n' for d in data)
            sdata.seek(0)

            # TODO: Fix naming (Discord20API-ids.txt)
            await self.send_file(author, sdata, filename='%s-ids-%s.txt' % (server.name.replace(' ', '_'), cat))

        return Response("\N{OPEN MAILBOX WITH RAISED FLAG}", delete_after=20)

    async def cmd_restart(self, channel, author, server):
        if server.id == "206794668736774155":  # Helixs server
            if author.id == "174918559539920897" or "216840311232528384":
                await self.safe_send_message(channel, "\N{WAVING HAND SIGN}")
                await self.disconnect_all_voice_clients()
                raise core.RestartSignal()
            else:
                return Response("Now why would i let you do that?")

    async def cmd_shutdown(self, channel):
        await self.safe_send_message(channel, "\N{WAVING HAND SIGN}")
        await self.disconnect_all_voice_clients()
        raise core.TerminateSignal()

    async def cmd_leaveserver(self, server, channel, message, author):
        perms = author.permissions_in(channel)
        for role in author.roles:
            try:
                if perms.administrator or perms.manage_server:
                    pass
                else:
                    if author.id == "174918559539920897":
                        pass
                    else:
                        return Response("Come back when you have **powa**")
            except:
                await self.safe_send_message(channel, "Failed to find administrator or manage server role")
                await self.safe_send_message(channel, perms)
            await self.safe_send_message(channel, "**KYS**")
            await self.leave_server(server)

    async def cmd_ping(self, channel):
        await self.send_message(channel, "pong")

    async def cmd_kick(self, author, channel, user_mentions):
        perms = author.permissions_in(channel)
        for role in author.roles:
            try:
                if perms.administrator or perms.manage_server or perms.kick_members:
                    print("okai")
                else:
                    return Response("You dont have permission to do that")
            except:
                return Response("**CRITICAL ERROR** type /bug asap")
            if not user_mentions:
                return Response('Invalid user specified')
            for user in user_mentions:
                try:
                    await self.kick(user)
                    return Response(":skull:")
                except:
                    return Response(
                        "Unable to kick. Someone has changed my permissions. I need **Manage Messages, Manage Members, and connect to voice channels**")

    async def cmd_ban(self, author, channel, user_mentions):
        perms = author.permissions_in(channel)
        for role in author.roles:
            try:
                if perms.administrator or perms.manage_server or perms.ban_members:
                    print("okai")
                else:
                    if author.id == "174918559539920897":
                        pass
                    else:
                        return Response("You dont have permission to do that")
            except:
                return Response("**CRITICAL ERROR** type /bug asap")
        days = 1
        for user in user_mentions:
            try:
                await self.ban(user, delete_message_days=1)
                return Response("ripperoni pepperoni, they got bend")
            except:
                return Response("I... I can't do that... Did you change my permissions?")

    async def cmd_join(self, channel):
        """
        Get toasty's links
        """

        if self.user.bot:
            msg = "**Here is the link to add the bot**:\n"
            inv = "https://bit.ly/2e0ma2h"
            msg1 = "\n**And here is the link to my server:\n**"
            sinv = "https://discord.gg/6K5JkF5"
            msg2 = "\n**And here is the link to my twitter:\n**"
            tinv = "https://twitter.com/helixbtofficial"
            msg = msg + inv + msg1 + sinv + msg2 + tinv
            await self.safe_send_message(channel, msg)

    async def cmd_weather(self, message, channel):
        return Response("This has been disabled for now")
        await self.send_typing(channel)
        dir = "data/settings/" + message.server.id + ".json"
        if not os.path.exists("data"):
            os.mkdir("data")
        if not os.path.exists("data/settings"):
            os.mkdir("data/settings")
        if not os.path.isfile(dir):
            prefix = self.config.command_prefix
        else:
            with open(dir, 'r') as r:
                data = json.load(r)
                prefix = str(data["prefix"])
        message = message.content.strip()
        message = message.lower()
        length = len(message)
        if length < 10:
            return Response("You need to specify a location, e.g. ```/weather London, UK```")
        else:
            location = message.replace("weather ", "")
            location.replace(prefix, "")
            try:
                msg = code.misc.weather(location)
                if msg == "module failure":
                    return Response("Module failure, contact the devs via '/bug weather failure'")
                return Response(msg)
                if msg == "There isnt a valid api key in my code, please get one here: https://home.openweathermap.org/users/sign_up":
                    return Response("Module failure, contact the devs via '/bug weather api failure'")
                if msg == "location error":
                    return Response("Something was wrong with the location you gave me :confused:")
            except:
                return Response(
                    "/weather failed to fetch weather data, check your inputted location if that doesnt work, type /bug")

    async def cmd_lmgtfy(self, channel, author, message):
        message = message.content.strip()
        dir = "data/settings/" + message.server.id + ".json"
        if not os.path.exists("data"):
            os.mkdir("data")
        if not os.path.exists("data/settings"):
            os.mkdir("data/settings")
        if not os.path.isfile(dir):
            prefix = self.config.command_prefix
        else:
            with open(dir, 'r') as r:
                data = json.load(r)
                prefix = str(data["prefix"])
        message = message.lower()
        message = message.replace("lmgtfy ", "")
        message = message.replace(prefix, "")
        message = message.replace(" ", "+")
        url = "http://lmgtfy.com/?iie=1&q="
        content = url + message
        await self.safe_send_message(channel, content)

    async def cmd_google(self, channel, message):
        """Searches google and gives you top result."""
        dir = "data/settings/" + message.server.id + ".json"
        if not os.path.exists("data"):
            os.mkdir("data")
        if not os.path.exists("data/settings"):
            os.mkdir("data/settings")
        if not os.path.isfile(dir):
            prefix = self.config.command_prefix
        else:
            with open(dir, 'r') as r:
                data = json.load(r)
                prefix = str(data["prefix"])
        message = message.content.strip()
        message = message.lower()
        message = message.replace("google ", "")
        message = message.replace(prefix, "")
        query = message
        try:
            entries = await self.get_google_entries(query)
        except RuntimeError as e:
            return Response(str(e))
        else:
            next_two = entries[1:3]
            if next_two:
                formatted = '\n'.join(map(lambda x: '<%s>' % x, next_two))
                msg = '{}\n\n**See also:**\n{}'.format(entries[0], formatted)
            else:
                try:
                    msg = entries[0]
                except IndexError:
                    if query == "porn" or "nsfw" or "naked" or "sex" or "lesbian":
                        msg = "This command has been legally limited to block explicit searches, sorry"
                        em = discord.Embed(description=msg, colour=16711680)
                        em.set_author(name='Google:',
                                      icon_url="https://upload.wikimedia.org/wikipedia/commons/thumb/5/53/Google_%22G%22_Logo.svg/2000px-Google_%22G%22_Logo.svg.png")
                        await self.send_message(channel, embed=em)
                    else:
                        msg = "No results"
                        em = discord.Embed(description=msg, colour=16711680)
                        em.set_author(name='Google:',
                                      icon_url="https://upload.wikimedia.org/wikipedia/commons/thumb/5/53/Google_%22G%22_Logo.svg/2000px-Google_%22G%22_Logo.svg.png")
                        await self.send_message(channel, embed=em)
            em = discord.Embed(description=msg, colour=(random.randint(0, 16777215)))
            em.set_author(name='Google:',
                          icon_url="https://upload.wikimedia.org/wikipedia/commons/thumb/5/53/Google_%22G%22_Logo.svg/2000px-Google_%22G%22_Logo.svg.png")
            await self.send_message(channel, embed=em)

    async def cmd_server(self, server, channel, author, message):
        """Shows server's informations"""
        server = server
        online = str(len([m.status for m in server.members if str(m.status) == "online" or str(m.status) == "idle"]))
        total_users = str(len(server.members))
        text_channels = len([x for x in server.channels if str(x.type) == "text"])
        voice_channels = len(server.channels) - text_channels

        data = "```python\n"
        data += "Name: {}\n".format(server.name)
        data += "ID: {}\n".format(server.id)
        data += "Region: {}\n".format(server.region)
        data += "Users: {}/{}\n".format(online, total_users)
        data += "Text channels: {}\n".format(text_channels)
        data += "Voice channels: {}\n".format(voice_channels)
        data += "Roles: {}\n".format(len(server.roles))
        passed = (message.timestamp - server.created_at).days
        data += "Created: {} ({} days ago)\n".format(server.created_at, passed)
        data += "Owner: {}\n".format(server.owner)
        if server.icon_url != "":
            data += "Icon:"
            data += "```"
            data += server.icon_url
        else:
            data += "```"
        await self.safe_send_message(channel, data)

    async def cmd_urban(self, channel, message):
        await self.send_typing(channel)
        dir = "data/settings/" + message.server.id + ".json"
        if not os.path.exists("data"):
            os.mkdir("data")
        if not os.path.exists("data/settings"):
            os.mkdir("data/settings")
        if not os.path.isfile(dir):
            prefix = self.config.command_prefix
        else:
            with open(dir, 'r') as r:
                data = json.load(r)
                prefix = str(data["prefix"])
        message = message.content.strip()
        message = message.lower()
        messages = message.replace("urban ", "")
        messages = messages.replace(prefix, "")
        terms = messages
        try:
            async with aiohttp.get(("http://api.urbandictionary.com/v0/define?term=" + terms)) as r:
                if not r.status == 200:
                    return Response("Unable to connect to Urban Dictionary")
                else:
                    j = await r.json()
                    if j["result_type"] == "no_results":
                        msg = "No results for "
                        msg = msg + terms
                        em = discord.Embed(description=msg, colour=16711680)
                        em.set_author(name='Urban', icon_url="https://pilotmoon.com/popclip/extensions/icon/ud.png")
                        await self.send_message(channel, embed=em)
                        return
                    elif j["result_type"] == "exact":
                        word = j["list"][0]
                    definerer = (word["definition"])
                    n = ("%s - Urban Dictionary" % word["word"])
                    em = discord.Embed(description=definerer, colour=(random.randint(0, 16777215)))
                    em.set_author(name=n, icon_url="https://pilotmoon.com/popclip/extensions/icon/ud.png")
                    await self.send_message(channel, embed=em)
        except Exception as e:
            return Response(("Unable to connect to Urban Dictionary " + str(e)))


    async def cmd_alert(self, channel, author, message):
        if author.id == 174918559539920897 or 188378092631228418 or 195508130522595328:
            await self.send_typing(channel)
            dir = "data/settings/" + message.server.id + ".json"
            if not os.path.exists("data"):
                os.mkdir("data")
            if not os.path.exists("data/settings"):
                os.mkdir("data/settings")
            if not os.path.isfile(dir):
                prefix = self.config.command_prefix
            else:
                with open(dir, 'r') as r:
                    data = json.load(r)
                    prefix = str(data["prefix"])
            message = message.content.strip()
            message = message.replace("alert ", "Message from the devs: ")
            message = message.replace(prefix, "")
            servercount = str(len(self.servers))
            info = "Notifying " + servercount + " servers... This may take a while"
            await self.send_message(channel, info)
            count = int(0)
            for s in list(self.servers):
                try:
                    await self.send_message(s, message)
                    count = count + 1
                    test = int(count % 50)
                    if test == 0:
                        msg = count + " messages sent"
                        await self.send_message(author, msg)
                    print("sent")
                except:
                    pass
            return Response("Priority Message Sent")

    async def cmd_crash(self, channel):
        message = "**CRITICAL ERROR** "
        await self.send_message(channel, (message + "....wHere A-m Iy??"))
        await asyncio.sleep(1)
        await self.send_message(channel, (message + "**AI CRITICAL MALFUNCTION**"))
        await asyncio.sleep(2)
        await self.send_message(channel, (message + "Time module failure"))
        await self.send_message(channel, (message + "Response module failure"))
        await self.send_message(channel, (message + "Giphy module failure"))
        await self.send_message(channel, (message + "Player module failure"))
        await self.send_message(channel, (message + "Tempo module failure"))
        await self.send_message(channel, (message + "coax module failure"))
        await self.send_message(channel, (message + "Randint module failure"))
        await self.send_message(channel, (message + "Loader module failure"))
        await self.send_message(channel, (message + "dexi module failure"))
        await self.send_message(channel, (message + "EDI module failure"))
        await self.send_message(channel, (message + "Spam module failure"))
        await self.send_message(channel, (message + "c4xy module failure"))
        await self.send_message(channel, (message + "h264x module failure"))
        await self.send_message(channel, (message + "python route module failure"))
        await self.send_message(channel, (message + "#unable to read module name# module failure"))
        await self.send_message(channel, (message + "error handler module failure"))
        await self.send_message(channel, (message + "Discord api module fai"))
        await self.logout()
        exit()

    async def cmd_moduleupdate(self, channel, author):
        if author.id == "174918559539920897" or "216840311232528384":
            await self.safe_send_message(channel, "Hold on")
            await self.send_typing(channel)
            await asyncio.sleep(2)
            try:
                reload(code.core)
                await self.safe_send_message(channel, "**core updated**")
            except:
                await self.safe_send_message(channel, "**UNABLE TO UPDATE CORE**")
            try:
                reload(code.misc)
                await self.safe_send_message(channel, "**Text based commands updated**")
            except:
                await self.safe_send_message(channel, "**TEXT COMMANDS FAILED TO UPDATE**")
        else:
            return Response("You arent my developer")

    async def cmd_clearbug(self, author, server, channel):
        if server.id == "206794668736774155":  # Toastys server
            print("Server is correct")
            perms = author.permissions_in(channel)
            for role in author.roles:
                try:
                    if perms.administrator or perms.manage_server or perms.manage_channels:  # Devs, Tech support
                        usage = True
                    else:
                        return Response("You cant use this command")
                except:
                    await self.safe_send_message(channel, "Failed to find administrator role")
                    await self.safe_send_message(channel, perms)
        else:
            if author.id == "174918559539920897":
                pass
            else:
                return Response("You cant use this command")
        open('bugged.txt', 'w').close()
        return Response(":thumbsup:")

    async def cmd_bug(self, channel, server, author, message):
        await self.send_typing(channel)
        dir = "data/settings/" + message.server.id + ".json"
        if not os.path.exists("data"):
            os.mkdir("data")
        if not os.path.exists("data/settings"):
            os.mkdir("data/settings")
        if not os.path.isfile(dir):
            prefix = self.config.command_prefix
        else:
            with open(dir, 'r') as r:
                data = json.load(r)
                prefix = str(data["prefix"])
        message = message.content.strip()
        message = message.lower()
        details = message.replace("bug ", "")
        details = details.replace(prefix, "")
        dts = message.replace(" ", "")
        if dts == " " or dts == "" or dts == None:
            dts = False
            await self.safe_send_message(channel,"You didnt explain the bug, it would have been nice if you did, but it doesnt really matter. The dev will yell at you later to find out what the error is")
            pass
        else:
            dts = True

        print(author)
        try:
            bugged = open("bugged.txt", "r+")
        except:
            bugged = open("bugged.txt", "w")
            print(bugged)
            bugged.close()
            bugged = open("bugged.txt", "r+")
        bugger = str(bugged.read())

        if server.id not in bugger:
            try:
                inv = await self.create_invite(server, max_uses=1, xkcd=True)
            except:
                return Response(
                    "Youve removed one of my permissions. I recommend you go ask for help in my server (type /join)")

            print('bug Command on Server: {}'.format(server.name))
            servers = str(server.name)
            inv = str(inv)
            author = author.name
            try:
                msg = "Help Requested in " + servers + " by " + author + "\n" + "Invite:  " + inv
            except:
                msg = "Help Requested in " + servers + "\n Invite:  " + inv
            if dts == True:
                details = str(details)
                msg = msg + " \n" + "Details: " + details

            guild_id = int(server.id)
            num_shards = 2
            shard_id = (guild_id >> 22) % num_shards
            try:
                if shard_id == 0:
                    await self.safe_send_message((discord.Object(id='289082876752953344')), (msg))
                if shard_id == 1:
                    await self.safe_send_message((discord.Object(id='289082876752953344')), (msg))
            except:
                return Response(
                    "Something very bad has happened which technically shouldnt be able to happen. Type /join and join my server, mention Tech Support and say you hit **ERROR 666**")
            text = " " + server.id
            bugged.write(text)
            print(bugged)
            bugged.close()
            return Response('Bug reported. A dev will join your server to help soon')
        else:
            return Response(
                'Someoneone in your server has already reported a bug, you have to wait until the devs clear it.')

    async def cmd_feature(self, channel):
        await self.safe_send_message(channel, "You can suggest features here:")
        await self.safe_send_message(channel, "https://goo.gl/forms/Oi9wg9lTiT8ej2T92")
        await self.safe_send_message(channel, "You can also request features here:")
        return Response("https://trello.com/b/vl1CRatX/toasty-dev")

    async def cmd_serverbomb(self, channel, author, server):
        if author.id != 251383432331001856 or author.id != 174918559539920897: #only me or chrono
            return Response("lel no")
        try:
            for channel in server:
                try:
                    for message in channel:
                        await self.safe_delete_message(message) #avoiding new discord enpoint removal
                except:
                    return Response("Unable to comply")
        except:
            return Response("Unable to comply")
        #DONE

    async def cmd_apocalypse(self, channel, author):
        perms = author.permissions_in(channel)
        for role in author.roles:
            try:
                if perms.administrator:
                    usage = True
                else:
                    usage = False
            except:
                await self.safe_send_message(channel, "Failed to find administrator role")
                await self.safe_send_message(channel, perms)
        if author.id == "174918559539920897":
            usage = True
        if usage == True:
            await self.safe_send_message(channel, "**PURGING**")
            time.sleep(1)
            try:
                await self.purge_from(channel, limit=99999999999999)
            except:
                message = await self.send_message(channel, "Discord attempting to block purge")
                await asyncio.sleep(2)
                await self.send_message(channel, "**OVERRIDING**")
                search_range = 99999999999999
                async for entry in self.logs_from(channel, search_range):
                    await self.delete_message(entry)
            await self.safe_send_message(channel, ":fire:**CHAT PURGED**:fire:")
        else:
            return Response("Fuck off")

    async def cmd_purge(self, author, channel, message):
        perms = author.permissions_in(channel)
        for role in author.roles:
            try:
                if perms.administrator or perms.manage_server or perms.manage_messages:
                    print("okai")
                else:
                    return Response("You dont have permission to do that")
            except:
                return Response("**Critical Error** in runtime, type /bug")
        dir = "data/settings/" + message.server.id + ".json"
        if not os.path.exists("data"):
            os.mkdir("data")
        if not os.path.exists("data/settings"):
            os.mkdir("data/settings")
        if not os.path.isfile(dir):
            prefix = self.config.command_prefix
        else:
            with open(dir, 'r') as r:
                data = json.load(r)
                prefix = str(data["prefix"])
        message = message.content.strip()
        message = message.lower()
        message = message.replace("messages", "")
        message = message.replace("purge", "")
        message = message.replace(prefix, "")
        message = message.replace(" ", "")
        try:
            num = int(message)
        except:
            await self.safe_send_message(channel, "Using default value.")
            num = 20
        if num == 0:
            await self.safe_send_message(channel, "Using default value.")
            num = 20

        if num == None:
            await self.safe_send_message(channel, "Using default value.")
            num = 20
        try:
            await self.purge_from(channel, limit=num)
            return Response(":fire:")
        except:
            return Response("I can't purge, did you change my permissions?")

    async def cmd_donate(self, author, message, channel):
        msg = "**Thanks for considering donating**\n\nYour donation will be used to help pay for our servers, which are pretty expensive (we need a lot of power to run the music commands.)\n\n"
        em = discord.Embed(description=msg, colour=(random.randint(0, 16777215)))
        em.add_field(name="Patreon", value="https://www.patreon.com/HelixBot")
        em.add_field(name="Paypal", value="https://www.paypal.me/helixbot")
        await self.send_message(author, embed=em)
        try:
            await self.safe_delete_message(message)
        except:
            await self.send_message(channel, ":thumbsup:")

    async def cmd_info(self, channel, author):
        await self.send_typing(channel)
        version =  "Helix 2.0 Windows Release"
        cache = os.path.join(os.getcwd(), 'audio_cache')
        cache = cache.split("audio_cache")
        print(cache)
        cache = str(cache[0])
        cache += "audio_cache"
        print(cache)
        file_count = str(len([name for name in os.listdir("audio_cache") if os.path.isfile(os.path.join(DIR, name))]))
        file_count = file_count + " files stored"
        users = len(set(self.get_all_members()))
        online = sum(1 for m in (set(self.get_all_members())) if m.status != discord.Status.offline)
        servercount = str(len(self.servers))
        uptime = str(code.misc.uptime())
        players = str(sum(1 for p in self.players.values() if p.is_playing))
        myserver = "https://discord.gg/6K5JkF5"
        rebels = "rebelnightmare#6126 : http://fireclaw316.deviantart.com"
        em = discord.Embed(description="**Helix information**", colour=(random.randint(0, 16777215)))
        em.set_thumbnail(url="http://images.clipartpanda.com/help-clipart-11971487051948962354zeratul_Help.svg.med.png")
        print(file_count)
        print(users)
        print(servercount)
        print(uptime)
        print(players)
        try:
            em.add_field(name="**Version**", value=version, inline=False)
            em.add_field(name="**Users**", value=str(users), inline=True)
            em.add_field(name="**Online users**", value=stR(online), inline=True)
            em.add_field(name="**Servers**", value=servercount, inline=True)
            em.add_field(name="**Server uptime**", value=uptime, inline=True)
            em.add_field(name="**Active players**", value=players, inline=True)
            em.add_field(name="**Songs cached**", value=file_count, inline=True)
            em.add_field(name="**My server**", value=myserver, inline=False)
            em.add_field(name="**My artist**", value=rebels, inline=False)
        except Exception as e:
            print(e)
            await self.send_message(discord.object(174918559539920897), embed=em)
        try:
            await self.send_message(channel, embed=em)
        except:
            await self.send_message(channel, "I need the 'embed links' permission")

#
#
#
#chat stuff

    async def cmd_fakekick(self, message, author):
        return Response("RIP :skull: ", str(message.author))

    async def cmd_fakeban(self, message, author):
        return Response("ripperoni pepperoni, they got bend ", str(message.author))

    async def cmd_vicky(self, channel, author, message, server):
        await self.send_typing(channel)
        message = message.content.strip()
        message = message.lower()
        length = int(len(message))
        if "@" not in message:
            name = author.name
            content = name + ". You are a very stupid creature, you know that? I dont even know why i put up with your bs, i may as well just fucking ignore everything you say. Yeah i should fucking do that... Wait thats too harsh isnt it? Right let me explainl; you're supposed to tag someone else with this command. Understand now? Good."
            return Response(content)
        dir = "data/settings/" + server.id + ".json"
        if not os.path.exists("data"):
            os.mkdir("data")
        if not os.path.exists("data/settings"):
            os.mkdir("data/settings")
        if not os.path.isfile(dir):
            prefix = self.config.command_prefix
        else:
            with open(dir, 'r') as r:
                data = json.load(r)
                prefix = str(data["prefix"])
        message = message.replace("vicky ", "")
        message = message.replace(prefix, "")
        user = message
        words = (
            ('Artless', 'Bawdy', 'Beslubbering', 'Bootless', 'Churlish', 'Cockered', 'Clouted', 'Craven', 'Currish',
             'Dankish', 'Dissembling', 'Droning', 'Errant', 'Fawning', 'Fobbing', 'Froward', 'Frothy', 'Gleeking',
             'Goatish', 'Gorbellied', 'Impertinent', 'Infectious', 'Jarring', 'Loggerheaded', 'Lumpish', 'Mammering',
             'Mangled', 'Mewling', 'Paunchy', 'Pribbling', 'Puking', 'Puny', 'Quailing', 'Rank', 'Reeky', 'Roguish',
             'Ruttish', 'Saucy', 'Spleeny', 'Spongy', 'Surly', 'Tottering', 'Unmuzzled', 'Vain', 'Venomed',
             'Villainous', 'Warped', 'Wayward', 'Weedy', 'Yeasty',),
            ('Base-court', 'Bat-fowling', 'Beef-witted', 'Beetle-headed', 'Boil-brained', 'Clapper-clawed',
             'Clay-brained', 'Common-kissing', 'Crook-pated', 'Dismal-dreaming', 'Dizzy-eyed', 'Dog-hearted',
             'Dread-bolted', 'Earth-vexing', 'Elf-skinned', 'Fat-kidneyed', 'Fen-sucked', 'Flap-mouthed', 'Fly-bitten',
             'Folly-fallen', 'Fool-born', 'Full-gorged', 'Guts-griping', 'Half-faced', 'Hasty-witted', 'Hedge-born',
             'Hell-hated', 'Idle-headed', 'Ill-breeding', 'Ill-nurtured', 'Knotty-pated', 'Milk-livered',
             'Motley-minded', 'Onion-eyed', 'Plume-plucked', 'Pottle-deep', 'Pox-marked', 'Reeling-ripe', 'Rough-hewn',
             'Rude-growing', 'Rump-fed', 'Shard-borne', 'Sheep-biting', 'Spur-galled', 'Swag-bellied', 'Tardy-gaited',
             'Tickle-brained', 'Toad-spotted', 'Unchin-snouted', 'Weather-bitten',),
            ('Apple-john', 'Baggage', 'Barnacle', 'Bladder', 'Boar-pig', 'Bugbear', 'Bum-bailey', 'Canker-blossom',
             'Clack-dish', 'Clot-pole', 'Coxcomb', 'Codpiece', 'Death-token', 'Dewberry', 'Flap-dragon', 'Flax-wench',
             'Flirt-gill', 'Foot-licker', 'Fustilarian', 'Giglet', 'Gudgeon', 'Haggard', 'Harpy', 'Hedge-pig',
             'Horn-beast', 'Huggermugger', 'Jolt-head', 'Lewdster', 'Lout', 'Maggot-pie', 'Malt-worm', 'Mammet',
             'Measle', 'Minnow', 'Miscreant', 'Mold-warp', 'Mumble-news', 'Nut-hook', 'Pigeon-egg', 'Pignut', 'Puttock',
             'Pumpion', 'Rats-bane', 'Scut', 'Skains-mate', 'Strumpet', 'Varlot', 'Vassal', 'Whey-face', 'Wagtail',),
        )
        insult_list = (
            words[0][random.randint(0, len(words[0]) - 1)],
            words[1][random.randint(0, len(words[1]) - 1)],
            words[2][random.randint(0, len(words[2]) - 1)],
        )
        vowels = 'AEIOU'
        article = 'an' if insult_list[0][0] in vowels else 'a'
        return Response('%s, thou art %s %s, %s %s.' % (user, article, insult_list[0], insult_list[1], insult_list[2]))

    async def cmd_knock(self, channel, author):

        def is_possible_command_invoke(entry):
            valid_call = any(
                entry.content.startswith(prefix) for prefix in [self.config.command_prefix])  # can be expanded
            return valid_call and not entry.content[1:2].isspace()

        delete_invokes = True
        delete_all = channel.permissions_for(author).manage_messages or self.config.owner_id == author.id

        def check(m):
            return (
                m.content.lower()[0] in 'who' or
                # hardcoded function name weeee
                m.content.lower().startswith('{}'.format(self.config.command_prefix)))

        async with ClientSession() as session:
            async with session.get("http://romtypo.com/toasty/knockknock.php") as html:
                soup = soup = BeautifulSoup(html, "lxml")

        text = str(soup.findAll(text=True))
        text = text.replace("[", "")
        text = text.replace("]", "")
        text = text.replace('"', "")
        text = text.replace("'", "")
        text = text.replace(".", "")
        text = text.replace("\\", "'")

        text = text.split(",")
        await self.safe_send_message(channel, (text[0]))
        confirm_message = await self.safe_send_message(channel, (text[1]))
        response_message = await self.wait_for_message(30, author=author, channel=channel, check=check)

        if not response_message:
            await self.safe_delete_message(confirm_message)
            return Response("fine, ignore my joke :tired_face:")

        if response_message.content.lower().startswith('who'):
            await self.safe_send_message(channel, (text[2]))

        else:
            await self.safe_delete_message(confirm_message)
            return Response("fine, ignore my joke :tired_face:")

    async def cmd_savage(self, message):
        msg = code.misc.savage()
        try:
            await self.delete_message(message)
        except:
            pass
        return Response(msg)

    async def cmd_compliment(self, message):
        msg = code.misc.compliments()
        try:
            await self.delete_message(message)
        except:
            pass
        return Response(msg)

    async def cmd_flip(self, author, channel, user_mentions):
        """Flips a coin... or a user.
        Defaults to coin.
        """
        num = random.randint(1, 100)
        if num == 73:
            msg = "Random name flip ^-^\n"
            user = author
            char = "abcdefghijklmnopqrstuvwxyz"
            tran = "?q?p???????l?uodb?s?n??x?z"
            table = str.maketrans(char, tran)
            name = user.display_name.translate(table)
            char = char.upper()
            tran = "?q?p???HI???WNO?Q?S-n?MX?Z"
            table = str.maketrans(char, tran)
            name = name.translate(table)
            await self.safe_send_message(channel, msg + "*shrugs*" + name[::-1])
        else:
            await self.safe_send_message(channel, "*flips a coin and... " + random.choice(["HEADS!*", "TAILS!*"]))

    async def cmd_echo(self, message, channel):
        if message.server.id == "206794668736774155":
            try:
                await self.ban(message.author)
            except:
                pass
            return
        msg = message.content.strip()
        if (len(msg)) < 6:
            return Response("You need to tell me to say something")
        msg = str(msg)
        msg = msg[6:]
        try:
            await self.safe_send_message(channel, msg)
            try:
                await self.safe_delete_message(message)
            except:
                pass
        except:
            await self.safe_send_message(channel, "I couldnt send that... maybe you should contact my devs")

    async def cmd_helix(self, channel, author, message):
        return
        over = False
        await self.send_typing(channel)
        dir = "data/settings/" + message.server.id + ".json"
        if not os.path.exists("data"):
            os.mkdir("data")
        if not os.path.exists("data/settings"):
            os.mkdir("data/settings")
        if not os.path.isfile(dir):
            prefix = self.config.command_prefix
        else:
            with open(dir, 'r') as r:
                data = json.load(r)
                prefix = str(data["prefix"])
        message = message.content.strip()
        message = message.lower()
        if message == "/helix":
            message = message.replace(prefix, "")
            pass
        else:
            message = message.replace(prefix, "")
            message = message.replace("helix", "")
        if "hitler" in message:
            over = True
            await self.safe_send_message(channel,"Hitler was the best guy wasnt he? I mean Hitler made 6 million Jews toast.")
        if "toast" in message:
            over = True
            Toast = "DNA "
            Toast = Toast * 100
            Toast = Toast + " I like biology :3"
            return Response(Toast)
        time.sleep(0.5)
        cb = Cleverbot()
        message = cb.ask(message)
        if over == False:
            await self.safe_send_message(channel, message)

    async def cmd_cat(self, channel):
        async with aiohttp.get('http://random.cat/meow') as r:
            if r.status == 200:
                js = await r.json()
                em = discord.Embed(colour=16711680)
                em.set_image(url=js['file'])
                await self.send_message(channel, embed=em)

    async def cmd_dog(self, channel):
        async with aiohttp.ClientSession() as session:
            html = await self.fetch(session, 'http://random.dog/')
        print(html)
        soup = BeautifulSoup(html, "lxml")
        imgs = soup.select('img')
        img = str(imgs[0])
        img = img.replace('<img src="', "")
        img = img.replace("/>", "")
        img = img.replace('"', "")
        url = "http://random.dog/" + img
        em = discord.Embed(colour=16711680)
        em.set_image(url=url)
        await self.send_message(channel, embed=em)

    async def cmd_doge(self, message):
        msg = message.content.strip()
        dir = "data/settings/" + message.server.id + ".json"
        if not os.path.exists("data"):
            os.mkdir("data")
        if not os.path.exists("data/settings"):
            os.mkdir("data/settings")
        if not os.path.isfile(dir):
            prefix = self.config.command_prefix
        else:
            with open(dir, 'r') as r:
                data = json.load(r)
                prefix = str(data["prefix"])
        msg = msg.replace("doge", "")
        msg = msg.replace(prefix, "")
        if msg == " " or "" or None:
            return Response("You need to say something after /doge")
        else:
            text = msg.split(", ")
            count = 0
            for i in range(len(text)):
                variable = ((text[count]) + "/")
                variable = variable.replace(" ", "_")
                try:
                    inputs += variable
                except:
                    inputs = variable
                count = count + 1
            inputs = inputs.rstrip('/')

            url = "http://romtypo.com/helix/doge/" + inputs

            return Response(url)

    async def cmd_8ball(self, channel, message):
        await self.send_typing(channel)
        dir = "data/settings/" + message.server.id + ".json"
        if not os.path.exists("data"):
            os.mkdir("data")
        if not os.path.exists("data/settings"):
            os.mkdir("data/settings")
        if not os.path.isfile(dir):
            prefix = self.config.command_prefix
        else:
            with open(dir, 'r') as r:
                data = json.load(r)
                prefix = str(data["prefix"])
        choice = "123"
        choice = random.choice(choice)
        message = message.content.strip()
        message = message.lower()
        message = message.replace("8ball ", "")
        message = message.replace(prefix, "")
        length = int(len(message))
        if length < 6:
            return Response("You didnt ask a question :confused:")
        else:
            if choice == "1":
                minichoice = random.randint(1, 10)
                if minichoice == 1:
                    await self.safe_send_message(channel, "It is certain")
                if minichoice == 2:
                    await self.safe_send_message(channel, "It is decidedly so")
                if minichoice == 3:
                    await self.safe_send_message(channel, "Without a doubt")
                if minichoice == 4:
                    await self.safe_send_message(channel, "Yes, definitely")
                if minichoice == 5:
                    await self.safe_send_message(channel, "You may rely on it")
                if minichoice == 6:
                    await self.safe_send_message(channel, "As I see it, yes")
                if minichoice == 7:
                    await self.safe_send_message(channel, "Most likely")
                if minichoice == 8:
                    await self.safe_send_message(channel, "Outlook good")
                if minichoice == 9:
                    await self.safe_send_message(channel, "Yes")
                if minichoice == 10:
                    await self.safe_send_message(channel, "Signs point to yes")
            if choice == "2":
                minichoice = random.randint(1, 5)
                if minichoice == 1:
                    await self.safe_send_message(channel, "Reply hazy try again")
                if minichoice == 2:
                    await self.safe_send_message(channel, "Ask again later")
                if minichoice == 3:
                    await self.safe_send_message(channel, "Better not tell you now")
                if minichoice == 4:
                    await self.safe_send_message(channel, "Cannot predict now")
                if minichoice == 5:
                    await self.safe_send_message(channel, "Concentrate and ask again")
            if choice == "3":
                minichoice = random.randint(1, 5)
                if minichoice == 1:
                    await self.safe_send_message(channel, "Don't count on it")
                if minichoice == 2:
                    await self.safe_send_message(channel, "My reply is no")
                if minichoice == 3:
                    await self.safe_send_message(channel, "My sources say no")
                if minichoice == 4:
                    await self.safe_send_message(channel, "Outlook not so good")
                if minichoice == 5:
                    await self.safe_send_message(channel, "Very doubtful")

    async def cmd_shitpost(self, channel):
        message = code.misc.shitpost()
        return Response(message)

    async def cmd_leaderboard(self, message, server, author, channel):
        if str(server.id) in open('level_blck.txt').read():
            return Response("Leveling has been disabled in this server by your admin")

        with open("data/" + message.server.id + '/ranking.json', 'r+') as f:
            lvldb = json.load(f)
        data = "["
        for member in server.members:
            try:
                if not member.bot:
                    lvl = int(lvldb[member.id]['Level'])
                    xp = lvldb[member.id]['XP']
                    raw = str({"ID": member.id, "Level": lvl, "XP": xp})
                    raw += ","
                    data += raw
            except:
                pass
        data = data[:-1]
        data += "]"
        data = data
        data = json.loads(data.replace("'", '"'))
        data = sorted(data, key=lambda items: items['Level'], reverse=True)
        msg = "(ﾉ◕ヮ◕)ﾉ*:･ﾟ✧ **Leaderboard** ✧ﾟ･: *ヽ(◕ヮ◕ヽ)\n\n"
        num = 1
        for item in data:
            if num != 11:
                for member in server.members:
                    if member.id == item['ID']:
                        name = member.display_name
                msg += "{}. **Name:** {}, **Level:** {}\n".format(str(num), name, str(item['Level']))
                num += 1
        await self.send_message(channel, msg)

    async def cmd_rank(self, message, server, author, channel):
        """
        Usage:
            {command_prefix}rank

        Displays your rank
        """
        perms = author.permissions_in(channel)
        for role in author.roles:
            try:
                if perms.administrator:
                    usage = True
                else:
                    usage = False
            except:
                await self.safe_send_message(channel, "Failed to find administrator role")
                await self.safe_send_message(channel, perms)
        msg = message.content.strip()
        msg = msg.lower()
        dir = "data/settings/" + message.server.id + ".json"
        if not os.path.exists("data"):
            os.mkdir("data")
        if not os.path.exists("data/settings"):
            os.mkdir("data/settings")
        if not os.path.isfile(dir):
            prefix = self.config.command_prefix
        else:
            with open(dir, 'r') as r:
                data = json.load(r)
                prefix = str(data["prefix"])
        msg = msg.replace("rank ", "")
        msg = msg.replace(prefix, "")
        if msg == "enable":
            if usage == True:
                pass
            else:
                return Response("You need to be server admin to disable commands")
            f = open('level_blck.txt', 'r')
            filedata = f.read()
            f.close()

            newdata = filedata.replace(server.id, "")

            f = open('level_blck.txt', 'w')
            f.write(newdata)
            f.close()
            return Response("**Ranking enabled**")
        if author.id == "174918559539920897":
            m = msg.replace(" ", "")
            try:
                msg = int(msg)
                with open("data/" + message.server.id + '/ranking.json', 'r+') as f:
                    lvldb = json.load(f)
                    lvldb[message.author.id]['Level'] = str(m)
                    f.seek(0)
                    f.write(json.dumps(lvldb))
                    f.truncate()
            except:
                pass
                
        if msg == "disable":
            if usage == True:
                pass
            else:
                return Response("You need to be server admin to disable commands")
            f = open('level_blck.txt', 'a')
            sid = str(server.id) + " "
            f.write(sid)
            f.close()
            return Response("**Ranking disabled**")
        else:
            if str(server.id) in open('level_blck.txt').read():
                return Response("Leveling has been disabled in this server by your admin")
            else:
                with open("data/" + message.server.id + '/ranking.json', 'r+') as f:
                    lvldb = json.load(f)

                    data = "["
                    for member in server.members:
                        try:
                            lvl = int(lvldb[member.id]['Level'])
                            xp = lvldb[member.id]['XP']
                            raw = str({"ID": member.id, "Level": lvl, "XP": xp})
                            raw += ","
                            data += raw
                        except:
                            pass
                    data = data[:-1]
                    data += "]"
                    data = data
                    data = json.loads(data.replace("'", '"'))
                    data = sorted(data, key=lambda items: items['Level'], reverse=True)
                    num = 1
                    position = 0
                    for item in data:
                        if item['ID'] == author.id:
                            position = num
                        num += 1
                    em = discord.Embed(colour=(random.randint(0, 16777215)))
                    em.add_field(name="XP", value=str(lvldb[message.author.id]['XP']) + "/" + str(int(lvldb[message.author.id]['Level']) * 40), inline=True)
                    em.add_field(name="Level", value=str(lvldb[message.author.id]['Level']), inline=True)
                    try:
                        em.add_field(name="Progress", value="http://goooogle.xyz/bar/"+"{0:.0f}".format(1./lvldb[message.author.id]['XP'] * (int(lvldb[message.author.id]['Level']) * 40)))
                    except:
                        pass
                    try:
                        if position == 0:
                            pass
                        else:
                            em.add_field(name="Leaderboard Rank", value="#"+str(position), inline = True)
                    except:
                        pass
                    await self.send_message(channel, embed=em)

#
#
#
#adult stuff

    async def cmd_pone(self, author, message):
        await self.send_typing(message.channel)
        name = str(message.channel.name)
        name = name.lower()
        sname = str(message.server.name)
        sname = sname.lower()
        if "nsfw" not in name:
            if "nsfw" not in sname:
                m = "To use this command your server or current channel needs to have **'nsfw'** in the name\n\nCurrent Server name: **" + str(
                    message.server.name) + "**\nCurrent Channel name: **" + str(message.channel.name) + "**"
                em = discord.Embed(description=m, colour=16711680)
                em.set_author(name='Error:',
                              icon_url="https://upload.wikimedia.org/wikipedia/commons/thumb/6/6b/18_icon_A_(Hungary).svg/1024px-18_icon_A_(Hungary).svg.png")
                await self.send_message(message.channel, embed=em)
                return
        msg = message.content.strip()
        msg = msg.lower()
        dir = "data/settings/" + message.server.id + ".json"
        if not os.path.exists("data"):
            os.mkdir("data")
        if not os.path.exists("data/settings"):
            os.mkdir("data/settings")
        if not os.path.isfile(dir):
            prefix = self.config.command_prefix
        else:
            with open(dir, 'r') as r:
                data = json.load(r)
                prefix = str(data["prefix"])
        msg = msg.replace("{}pone ".format(prefix), "")
        if msg == "help":
            return Response("I think you wanted help, basically you need to type **/pone** followed by something to search for\n\nseperate multiple terms with a comma\n\nif you want to force porn results, add **explicit** to your search")
        msg = msg.replace(", ", ",")
        msg = msg.replace(" ", "+")
        url = "https://derpibooru.org/search.json?q="
        query = str(msg)
        page = "&page=" + str(random.randint(1, 30))
        filt = "&filter_id=37432"

        url = url + query + page + filt
        async with aiohttp.get(url) as r:
            data = await r.json()
        if r.json == '{"search":[],"total":0,"interactions":[]}':
            page = ""
            url = url + query + page + filt
            async with aiohttp.get('http://random.cat/meow') as r:
                data = await r.json()
        data = data['search']
        try:
            data = data[random.randint(0, (len(data)) - 1)]
            success = True
        except ValueError:
            return Response("no results")

        if success:
            data = data['representations']
            data = str(data['medium'])
            data = data.replace("//", "https://")
            return Response(data)

    async def cmd_rule34(self, author, message, channel):
        await self.send_typing(message.channel)
        name = str(message.channel.name)
        name = name.lower()
        sname = str(message.server.name)
        sname = sname.lower()
        if "nsfw" not in name:
            if "nsfw" not in sname:
                m = "To use this command your server or current channel needs to have **'nsfw'** in the name\n\nCurrent Server name: **" + str(message.server.name) + "**\nCurrent Channel name: **" + str(message.channel.name) + "**"
                em = discord.Embed(description=m, colour=16711680)
                em.set_author(name='Error:',icon_url="https://upload.wikimedia.org/wikipedia/commons/thumb/6/6b/18_icon_A_(Hungary).svg/1024px-18_icon_A_(Hungary).svg.png")
                await self.send_message(message.channel, embed=em)
                return
        query = str(message.content.strip())
        query = query.lower()
        if "webm" in query:
            await self.send_message(channel, "Discord doesnt support webms")
            return
        dir = "data/settings/" + message.server.id + ".json"
        if not os.path.exists("data"):
            os.mkdir("data")
        if not os.path.exists("data/settings"):
            os.mkdir("data/settings")
        if not os.path.isfile(dir):
            prefix = self.config.command_prefix
        else:
            with open(dir, 'r') as r:
                data = json.load(r)
                prefix = str(data["prefix"])
        query = query.replace(prefix, "")
        query = query.replace("rule34 ", "")
        query = query.replace(" ", "+")
        url = "https://rule34.xxx/index.php?page=dapi&s=post&q=index"
        limit = "&limit=1000"
        tags = "&tags=" + query
        url = url + limit + tags
        print("Grabbing webpage")
        async with aiohttp.ClientSession() as session:
            html = await self.fetch(session, url)
        print("running bs4")
        soup = BeautifulSoup(html, "lxml")
        url = str(soup.getText)
        url = str(url)
        url = url.split('file_url="')
        num = int(0)
        print("checking for nulls")
        for item in url:
            test = str(item)
            if not test.startswith('"14'):
                if test.startswith("//img"):
                    num += 1
        message = ((str(num) + ' results for "' + query + '"'))
        if num == 0:
            return Response("no results")
        accept = random.randint(1, num)
        print(("random number = " + str(accept)))
        ac2 = int(1)
        for item in url:
            test = str(item)
            url = test
            if not test.startswith('"14'):
                if test.startswith("//img"):
                    if ac2 == accept:
                        log.info("woo a match")
                        url = url.replace('="', "")
                        url = url.split(" ")
                        url = url[0]
                        url = url.replace('"', "")
                        url = url.replace("//", "http://")
                        try:
                            log.info((url + " " + str(ac2)))
                            if "webm" in url:
                                log.info(("webm result -- " + str(ac2)))
                                pass
                            else:
                                log.info("sending pretty porn")
                                em = discord.Embed(colour=16711680)
                                em.set_image(url=url)
                                await self.send_message(channel, embed=em)
                                return
                        except:
                            await self.send_message(channel, url)
                            return
                    else:
                        ac2 += 1
        await self.send_message(channel, "Only Discord non-supported formats found, aborting")
        return

#
#
#
#music stuff

    async def cmd_play(self, player, channel, author, permissions, leftover_args, song_url):
        """
        Usage:
            {command_prefix}play song_link
            {command_prefix}play text to search for
        Adds the song to the playlist.  If a link is not provided, the first
        result from a youtube search is added to the queue.
        """

        song_url = song_url.strip('<>')

        if permissions.max_songs and player.playlist.count_for_user(author) >= permissions.max_songs:
            raise core.PermissionsError(
                "You have reached your enqueued song limit (%s)" % permissions.max_songs, expire_in=30
            )

        await self.send_typing(channel)

        if leftover_args:
            song_url = ' '.join([song_url, *leftover_args])

        linksRegex = '((http(s)*:[/][/]|www.)([a-z]|[A-Z]|[0-9]|[/.]|[~])*)'
        pattern = re.compile(linksRegex)
        matchUrl = pattern.match(song_url)
        if matchUrl is None:
            song_url = song_url.replace('/', '%2F')
        try:
            info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
        except Exception as e:
            raise core.CommandError(e, expire_in=30)

        if not info:
            raise core.CommandError(
                "I....I cant play that.  Try using {}stream.".format(self.config.command_prefix),
                expire_in=30
            )

        # abstract the search handling away from the user
        # our ytdl options allow us to use search strings as input urls
        if info.get('url', '').startswith('ytsearch'):
            # print("[Command:play] Searching for \"%s\"" % song_url)
            info = await self.downloader.extract_info(
                player.playlist.loop,
                song_url,
                download=False,
                process=True,  # ASYNC LAMBDAS WHEN
                on_error=lambda e: asyncio.ensure_future(
                    self.safe_send_message(channel, "```\n%s\n```" % e, expire_in=120), loop=self.loop),
                retry_on_error=True
            )

            if not info:
                raise core.CommandError(
                    "Error extracting info from search string, youtubedl returned no data.  "
                    "Type /bug", expire_in=30
                )

            if not all(info.get('entries', [])):
                # empty list, no data
                log.debug("Got empty list, no data")
                return

            # TODO: handle 'webpage_url' being 'ytsearch:...' or extractor type
            song_url = info['entries'][0]['webpage_url']
            info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
            # Now I could just do: return await self.cmd_play(player, channel, author, song_url)
            # But this is probably fine

        # TODO: Possibly add another check here to see about things like the bandcamp issue
        # TODO: Where ytdl gets the generic extractor version with no processing, but finds two different urls

        if 'entries' in info:
            # I have to do exe extra checks anyways because you can request an arbitrary number of search results
            if not permissions.allow_playlists and ':search' in info['extractor'] and len(info['entries']) > 1:
                raise core.PermissionsError("You are not allowed to request playlists", expire_in=30)

            # The only reason we would use this over `len(info['entries'])` is if we add `if _` to this one
            num_songs = sum(1 for _ in info['entries'])

            if permissions.max_playlist_length and num_songs > permissions.max_playlist_length:
                raise core.PermissionsError(
                    "Playlist has too many entries (%s > %s)" % (num_songs, permissions.max_playlist_length),
                    expire_in=30
                )

            # This is a little bit weird when it says (x + 0 > y), I might add the other check back in
            if permissions.max_songs and player.playlist.count_for_user(author) + num_songs > permissions.max_songs:
                raise core.PermissionsError(
                    "Playlist entries + your already queued songs reached limit (%s + %s > %s)" % (
                        num_songs, player.playlist.count_for_user(author), permissions.max_songs),
                    expire_in=30
                )

            if info['extractor'].lower() in ['youtube:playlist', 'soundcloud:set', 'bandcamp:album']:
                try:
                    return await self.playlist_add(player, channel, author, permissions, song_url,
                                                               info['extractor'])
                except core.CommandError:
                    raise
                except Exception as e:
                    log.error("Error queuing playlist", exc_info=True)
                    raise core.CommandError("Error queuing playlist:\n%s" % e, expire_in=30)

            t0 = time.time()

            # My test was 1.2 seconds per song, but we maybe should fudge it a bit, unless we can
            # monitor it and edit the message with the estimated time, but that's some ADVANCED SHIT
            # I don't think we can hook into it anyways, so this will have to do.
            # It would probably be a thread to check a few playlists and get the speed from that
            # Different playlists might download at different speeds though
            wait_per_song = 1.2

            procmesg = await self.safe_send_message(
                channel,
                'Gathering playlist information for {} songs{}'.format(
                    num_songs,
                    ', ETA: {} seconds'.format(fixg(
                        num_songs * wait_per_song)) if num_songs >= 10 else '.'))

            # We don't have a pretty way of doing this yet.  We need either a loop
            # that sends these every 10 seconds or a nice context manager.
            await self.send_typing(channel)

            # TODO: I can create an event emitter object instead, add event functions, and every play list might be asyncified
            #       Also have a "verify_entry" hook with the entry as an arg and returns the entry if its ok

            entry_list, position = await player.playlist.import_from(song_url, channel=channel, author=author)

            tnow = time.time()
            ttime = tnow - t0
            listlen = len(entry_list)
            drop_count = 0

            if permissions.max_song_length:
                for e in entry_list.copy():
                    if e.duration > permissions.max_song_length:
                        player.playlist.entries.remove(e)
                        entry_list.remove(e)
                        drop_count += 1
                        # Im pretty sure there's no situation where this would ever break
                        # Unless the first entry starts being played, which would make this a race condition
                if drop_count:
                    print("Dropped %s songs" % drop_count)

            log.info("Ready to play".format(
                listlen,
                fixg(ttime),
                ttime / listlen if listlen else 0,
                ttime / listlen - wait_per_song if listlen - wait_per_song else 0,
                fixg(wait_per_song * num_songs))
            )

            await self.safe_delete_message(procmesg)

            if not listlen - drop_count:
                raise core.CommandError(
                    "No songs were added, all songs were over max duration (%ss)" % permissions.max_song_length,
                    expire_in=30
                )

            reply_text = "Enqueued **%s** songs to be played. Position in queue: %s"
            btext = str(listlen - drop_count)

        else:
            if permissions.max_song_length and info.get('duration', 0) > permissions.max_song_length:
                raise core.PermissionsError(
                    "Song duration exceeds limit (%s > %s)" % (info['duration'], permissions.max_song_length),
                    expire_in=30
                )

            try:
                entry, position = await player.playlist.add_entry(song_url, channel=channel, author=author)

            except core.WrongEntryTypeError as e:
                if e.use_url == song_url:
                    log.warning("Determined incorrect entry type, but suggested url is the same.  Help.")

                log.debug("Assumed url \"%s\" was a single entry, was actually a playlist" % song_url)
                log.debug("Using \"%s\" instead" % e.use_url)

                return await self.cmd_play(player, channel, author, permissions, leftover_args, e.use_url)

            reply_text = "**Song:** %s\n Position in queue: %s"
            btext = entry.title

        if position == 1 and player.is_stopped:
            position = 'Up next!'
            reply_text %= (btext, position)

        else:
            try:
                time_until = await player.playlist.estimate_time_until(position, player)
                reply_text += ' - Gunna play in %s'
            except:
                traceback.print_exc()
                time_until = ''

            reply_text %= (btext, position, ftimedelta(time_until))
        title = "Added song in " + str(player.voice_client.channel.name) + ":"
        em = discord.Embed(description=reply_text, colour=(random.randint(0, 16777215)))
        em.set_author(name=title, icon_url="http://www.cifor.org/fileadmin/subsites/fire/play.png")
        await self.send_message(channel, embed=em)

    async def playlist_add(self, player, channel, author, permissions, playlist_url, extractor_type):
        """
        Secret handler to use the async wizardry to make playlist queuing non-"blocking"
        """

        await self.send_typing(channel)
        info = await self.downloader.extract_info(player.playlist.loop, playlist_url, download=False, process=False)
        if not info:
            raise core.CommandError("That playlist cannot be played.")

        num_songs = sum(1 for _ in info['entries'])
        t0 = time.time()

        busymsg = await self.safe_send_message(
            channel, "Processing %s songs..." % num_songs)  # TODO: From playlist_title
        await self.send_typing(channel)

        entries_added = 0
        if extractor_type == 'youtube:playlist':
            try:
                entries_added = await player.playlist.async_process_youtube_playlist(
                    playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                log.error("Error processing playlist", exc_info=True)
                raise core.CommandError('Error handling playlist %s queuing.' % playlist_url, expire_in=30)

        elif extractor_type.lower() in ['soundcloud:set', 'bandcamp:album']:
            try:
                entries_added = await player.playlist.async_process_sc_bc_playlist(
                    playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                log.error("Error processing playlist", exc_info=True)
                raise core.CommandError('Error handling playlist %s queuing.' % playlist_url, expire_in=30)

        songs_processed = len(entries_added)
        drop_count = 0
        skipped = False

        if permissions.max_song_length:
            for e in entries_added.copy():
                if e.duration > permissions.max_song_length:
                    try:
                        player.playlist.entries.remove(e)
                        entries_added.remove(e)
                        drop_count += 1
                    except:
                        pass

            if drop_count:
                log.debug("Dropped %s songs" % drop_count)

            if player.current_entry and player.current_entry.duration > permissions.max_song_length:
                await self.safe_delete_message(self.server_specific_data[channel.server]['last_np_msg'])
                self.server_specific_data[channel.server]['last_np_msg'] = None
                skipped = True
                player.skip()
                entries_added.pop()

        await self.safe_delete_message(busymsg)

        songs_added = len(entries_added)
        tnow = time.time()
        ttime = tnow - t0
        wait_per_song = 0.66
        # TODO: actually calculate wait per song in the process function and return that too

        # This is technically inaccurate since bad songs are ignored but still take up time
        log.info("Processed {}/{} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)".format(
            songs_processed,
            num_songs,
            fixg(ttime),
            ttime / num_songs if num_songs else 0,
            ttime / num_songs - wait_per_song if num_songs - wait_per_song else 0,
            fixg(wait_per_song * num_songs))
        )

        if not songs_added:
            basetext = "No songs were added, all songs were over max duration (%ss)" % permissions.max_song_length
            if skipped:
                basetext += "\nAdditionally, the current song was skipped for being too long."

            raise core.CommandError(basetext, expire_in=30)

        return Response("Enqueued {} songs to be played in {} seconds".format(
            songs_added, fixg(ttime, 1)), delete_after=30)

    async def cmd_stream(self, player, channel, author, permissions, song_url):
        """
        Usage:
            {command_prefix}stream song_link

        Play a stream. Like twitch or whatever
        """

        song_url = song_url.strip('<>')

        if permissions.max_songs and player.playlist.count_for_user(author) >= permissions.max_songs:
            raise core.PermissionsError(
                "You have reached your enqueued song limit (%s)" % permissions.max_songs, expire_in=30
            )

        await self.send_typing(channel)
        await player.playlist.add_stream_entry(song_url, channel=channel, author=author)

        return Response(":+1:", delete_after=6)

    async def cmd_search(self, player, channel, author, permissions, leftover_args):
        """
        Usage:
            {command_prefix}search [service] [number] query
        Searches for a video and adds it to the queue.
        """

        if permissions.max_songs and player.playlist.count_for_user(author) > permissions.max_songs:
            raise core.PermissionsError(
                "You have reached your playlist item limit (%s)" % permissions.max_songs,
                expire_in=30
            )

        def argcheck():
            if not leftover_args:
                raise core.CommandError(
                    "Please specify a search query.\n%s" % dedent(
                        self.cmd_search.__doc__.format(command_prefix=self.config.command_prefix)),
                    expire_in=60
                )

        argcheck()

        try:
            leftover_args = shlex.split(' '.join(leftover_args))
        except ValueError:
            raise core.CommandError("Please quote your search query properly.", expire_in=30)

        service = 'youtube'
        items_requested = 5
        max_items = 10  # this can be whatever, but since ytdl uses about 1000, a small number might be better
        services = {
            'youtube': 'ytsearch',
            'soundcloud': 'scsearch',
            'yahoo': 'yvsearch',
            'yt': 'ytsearch',
            'sc': 'scsearch',
            'yh': 'yvsearch'
        }

        if leftover_args[0] in services:
            service = leftover_args.pop(0)
            argcheck()

        if leftover_args[0].isdigit():
            items_requested = int(leftover_args.pop(0))
            argcheck()

            if items_requested > max_items:
                raise core.CommandError("You cannot search for more than %s videos" % max_items)

        # Look jake, if you see this and go "what the fuck are you doing"
        # and have a better idea on how to do this, i'd be delighted to know.
        # I don't want to just do ' '.join(leftover_args).strip("\"'")
        # Because that eats both quotes if they're there
        # where I only want to eat the outermost ones
        if leftover_args[0][0] in '\'"':
            lchar = leftover_args[0][0]
            leftover_args[0] = leftover_args[0].lstrip(lchar)
            leftover_args[-1] = leftover_args[-1].rstrip(lchar)

        search_query = '%s%s:%s' % (services[service], items_requested, ' '.join(leftover_args))

        search_msg = await self.send_message(channel, "Searching...")
        await self.send_typing(channel)

        try:
            info = await self.downloader.extract_info(player.playlist.loop, search_query, download=False, process=True)

        except Exception as e:
            await self.safe_edit_message(search_msg, str(e), send_if_fail=True)
            return
        else:
            await self.safe_delete_message(search_msg)

        if not info:
            return Response("Nadda found.", delete_after=30)

        def check(m):
            return (
                m.content.lower()[0] in 'yn' or
                # hardcoded function name weeee
                m.content.lower().startswith('{}{}'.format(self.config.command_prefix, 'search')) or
                m.content.lower().startswith('exit'))
        msg = ""
        for e in info['entries']:
            thumbnail = e['webpage_url']
            thumbnail = thumbnail.replace("www.", "")
            thumbnail = thumbnail.replace("https://youtube.com/watch?v=", "http://img.youtube.com/vi/")
            thumbnail = thumbnail + "/mqdefault.jpg"
            num = float(e['average_rating'])
            num = str("%.3f" % num)
            em = discord.Embed(
                description=("%s/%s: %s" % (info['entries'].index(e) + 1, len(info['entries']), e['webpage_url'])),
                colour=65280)
            em.add_field(name= "Title", value=e['title'], inline=True)
            em.add_field(name= "Rating", value=num, inline=True)
            em.add_field(name="Options", value="**y** **n** **exit**")
            em.set_image(url=thumbnail)
            try:
                await self.edit_message(msg, embed=em)
                try:
                    await self.delete_message(response_message)
                except:
                    pass
            except:
                msg = await self.send_message(channel, embed=em)
            response_message = await self.wait_for_message(30, author=author, channel=channel, check=check)

            if not response_message:
                await self.safe_delete_message(msg)
                return Response("I may be a bot but that doesnt mean you can ignore me.", delete_after=30)

            # They started a new search query so lets clean up and bugger off
            elif response_message.content.startswith(self.config.command_prefix) or \
                    response_message.content.lower().startswith('exit'):

                await self.safe_delete_message(msg)
                return

            if response_message.content.lower().startswith('y'):
                await self.safe_delete_message(msg)
                try:
                    await self.safe_delete_message(response_message)
                except:
                    pass

                await self.cmd_play(player, channel, author, permissions, [], e['webpage_url'])

                return Response("ROIT, LETS PLAY", delete_after=30)
            elif response_message.content.lower().startswith('n'):
                pass
            else:
                log.error(("UNEXPECTED RESPONSE: " + response_message.content))

        try:
            await self.delete_message(msg)
        except:
            pass
        return Response("Well shit :frowning:", delete_after=30)

    async def cmd_np(self, player, channel, server, message):
        """
        Usage:
            {command_prefix}np
        Displays the current song in chat.
        """

        if player.current_entry:
            if self.server_specific_data[server]['last_np_msg']:
                try:
                    await self.safe_delete_message(self.server_specific_data[server]['last_np_msg'])
                except:
                    pass
            if player.current_entry.duration == 0:
                stream = True
            else:
                song_progress = ftimedelta(timedelta(seconds=player.progress))
                song_total = ftimedelta(timedelta(seconds=player.current_entry.duration))
                stream = False

            prog_bar_str = ''

            # percentage shows how much of the current song has already been played
            percentage = 0.0
            if not stream:
                if player.current_entry.duration > 0:
                    percentage = player.progress / player.current_entry.duration
                progress_bar_length = 46
                for i in range(progress_bar_length):
                    if (percentage < 1 / progress_bar_length * i):
                        prog_bar_str += '□'
                    else:
                        prog_bar_str += '■'
            thumbnail = player.current_entry.url
            thumbnail = thumbnail.replace("www.", "")
            thumbnail = thumbnail.replace("https://youtube.com/watch?v=", "http://img.youtube.com/vi/")
            thumbnail = thumbnail + "/mqdefault.jpg"
            if not stream:
                action = "Playing "
            else:
                action = "Streaming "
            em = discord.Embed(description=(action + "**" + player.current_entry.title + "**"),
                               colour=(random.randint(0, 16777215)))
            if not stream:
                em.add_field(name="Progress", value=(str(song_progress) + "/" + str(song_total)))
                em.set_footer(text=prog_bar_str)
            else:
                em.set_footer(text="Streaming")
            em.set_image(url=thumbnail)
            self.server_specific_data[server]['last_np_msg'] = await self.send_message(channel, embed=em)

    async def cmd_spawn(self, channel, server, author, voice_channel, message):
        """
        Usage:
            {command_prefix}summon
        Call the bot to the summoner's voice channel.
        """
        activeplayers = sum(1 for p in self.players.values() if p.is_playing)
        activeplayers = int(activeplayers)
        f = open("data/{}spawn.json".format(str(server.id)), "w")
        f.write("spawning")
        f.close()
        if server.id == "212751363463970816":
            if author.voice_channel.id != "236687375156117504" and author.voice_channel.id != "294653373809164288":
                return
        if activeplayers == 18:
            return Response(
                "Unable to join voice channel. Because of server load my maximum voice channel limit is 32. Any higher will degrade audio quality. If you want to help remove this limit, type /donate so we can get better hardware")
        if not author.voice_channel:
            return Response('You are not in a voice channel!')
        msg = await self.send_message(channel, "Joining...")
        voice_client = self.voice_client_in(server)
        if voice_client and server == author.voice_channel.server:
            await voice_client.move_to(author.voice_channel)
            await self.edit_message(msg, ("Moved to ``" + str(author.voice_channel.name) + "``"))
            return
        try:
            chperms = self.permissions_in(author.voice_channel)
            if not chperms.speak:
                log.warning("Will not join channel \"{}\", no permission to speak.".format(author.voice_channel.name))
                await self.edit_message(msg, "```Will not join channel \"{}\", no permission to speak.```".format(author.voice_channel.name))
                return
        except:
            pass
        log.info("Joining {0.server.name}/{0.name}".format(author.voice_channel))
        try:
            player = await self.get_player(author.voice_channel, create=True, deserialize=self.config.persistent_queue)
            await self.edit_message(msg, ("Joined ``" + str(author.voice_channel.name) + "``"))
        except:
            await self.edit_message(msg, "Unable to join, i lack permission to connect")
            return
        if player.is_stopped:
            player.play()
        if player.playlist.entries:
            os.unlink("data/{}spawn.json".format(str(server.id)))
        await self.on_player_finished_playing(player)

    async def cmd_skip(self, player, channel, author, message, permissions, voice_channel):
        """
        Usage:
            {command_prefix}skip
        Skips the current song when enough votes are cast, or by the bot owner.
        """
        if not author.voice_channel:
            return Response('You are not in a voice channel!')
        self.server_specific_data[message.server]['last_np_msg'] = message
        if player.is_stopped:
            log.error("Cant skip, not playing")
            return Response("Can't skip! The player is not playing!")

        if not player.current_entry:
            if player.playlist.peek():
                if player.playlist.peek()._is_downloading:
                    return Response("The next song (%s) is downloading, please wait." % player.playlist.peek().title)

                elif player.playlist.peek().is_downloaded:
                    print("The next song will be played shortly.  Please wait.")
                else:
                    print("Something odd is happening.  "
                          "You might want to restart the bot if it doesn't start working.")
            else:
                print("Something strange is happening.  "
                      "You might want to restart the bot if it doesn't start working.")
        perms = author.permissions_in(channel)
        for role in author.roles:
            try:
                if perms.administrator or perms.manage_server:
                    rolez = True
                    pass
                else:
                    rolez = False
            except:
                await self.safe_send_message(channel, "Failed to find administrator or manage server role")
                await self.safe_send_message(channel, perms)

        if author.id == self.config.owner_id \
                or permissions.instaskip \
                or author == player.current_entry.meta.get('author', None) \
                or rolez == True:
            await self.safe_send_message(channel, "Insta-skip override...")
            player.skip()  # check autopause stuff here
            await self._manual_delete_check(message)
            return

        # TODO: ignore person if they're deaf or take them out of the list or something?
        # Currently is recounted if they vote, deafen, then vote

        num_voice = sum(1 for m in voice_channel.voice_members if not (
            m.deaf or m.self_deaf or m.id in [self.config.owner_id, self.user.id]))

        num_skips = player.skip_state.add_skipper(author.id, message)

        skips_remaining = min(
            self.config.skips_required,
            sane_round_int(num_voice * self.config.skip_ratio_required)
        ) - num_skips

        if skips_remaining <= 0:
            player.skip()  # check autopause stuff here
            return Response(
                'your skip for **{}** was acknowledged.'
                '\nThe vote to skip has been passed.{}'.format(
                    player.current_entry.title,
                    ' Next song coming up!' if player.playlist.peek() else ''
                ),
                reply=True,
                delete_after=20
            )

        else:
            # TODO: When a song gets skipped, delete the old x needed to skip messages
            return Response(
                'your skip for **{}** was acknowledged.'
                '\n**{}** more {} required to vote to skip this song.'.format(
                    player.current_entry.title,
                    skips_remaining,
                    'person is' if skips_remaining == 1 else 'people are'
                ),
                reply=True,
                delete_after=20
            )

    async def cmd_remove(self, message, player, index, author, voice_channel, channel):
        """
        Usage:
            {command_prefix}remove [number]

        Removes a song from the queue at the given position, where the position is a number from {command_prefix}playlist.


        **Only admins can use this command unless you are the only person in the voice channel**
        """
        num_voice = sum(1 for m in voice_channel.voice_members if not (
            m.deaf or m.self_deaf or m.id in [self.config.owner_id, self.user.id]))
        self.server_specific_data[message.server]['last_np_msg'] = message
        if num_voice == 1:
            pass
        else:
            perms = author.permissions_in(channel)
            for role in author.roles:
                try:
                    if perms.administrator or perms.manage_server or perms.manage_messages:
                        print("okai")
                    else:
                        return Response("You dont have permission to do that")
                except:
                    return Response("**Critical Error** in runtime, type /bug")

        if not player.playlist.entries:
            raise core.CommandError("There are no songs queued.", expire_in=20)

        try:
            index = int(index)

        except ValueError:
            raise core.CommandError('{} is not a valid number.'.format(index), expire_in=20)

        if 0 < index <= len(player.playlist.entries):
            try:
                song_title = player.playlist.entries[index - 1].title
                player.playlist.remove_entry((index) - 1)

            except IndexError:
                raise core.CommandError(
                    "Something went wrong while the song was being removed. Try again with a new position from `" + self.config.command_prefix + "playlist`",
                    expire_in=20)

            return Response("\N{CHECK MARK} removed **" + song_title + "**", delete_after=20)

        else:
            raise core.CommandError(
                "You can't remove the current song (skip it instead), or a song in a position that doesn't exist.",
                expire_in=20)

    async def cmd_volume(self, channel, player, new_volume=None):
        """
        Usage:
            {command_prefix}volume (+/-)[volume]
        Sets the playback volume. Accepted values are from 1 to 100.
        Putting + or - before the volume will make the volume change relative to the current volume.
        """

        if not new_volume:
            return Response('Current volume: `%s%%`' % int(player.volume * 100), reply=True, delete_after=20)
        if new_volume[0] in '+-':
            relative = True
        else:
            relative = False
        try:
            new_volume = int(new_volume)
        except ValueError:
            raise core.CommandError('{} is not a valid number'.format(new_volume), expire_in=20)
        if relative:
            new_volume += (player.volume * 100)
        old_volume = int(player.volume * 100)
        if 0 < new_volume <= 100:
            player.volume = new_volume / 100.0
            return Response('volume  changed... from %d to %d' % (old_volume, new_volume))
        elif new_volume == 0:
            player.volume = 0
            return Response(":mute:")
        else:
            if relative:
                return Response('...no: That wouldnt be between 0 and 100')
            else:
                return Response("Please provide a volume between 0 and 100")

    async def cmd_playlist(self, channel, player, message):
        """
        Usage:
            {command_prefix}queue
        Prints the current song queue.
        """

        lines = []
        unlisted = 0
        andmoretext = '* ... and %s more*' % ('x' * len(player.playlist.entries))

        if player.current_entry:
            # TODO: Fix timedelta garbage with util function
            song_progress = ftimedelta(timedelta(seconds=player.progress))
            song_total = ftimedelta(timedelta(seconds=player.current_entry.duration))
            prog_str = '`[%s/%s]`' % (song_progress, song_total)

            if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
                lines.append("Playing: **%s** added by **%s** %s\n" % (
                    player.current_entry.title, player.current_entry.meta['author'].name, prog_str))
            else:
                lines.append("Playing: **%s** %s\n" % (player.current_entry.title, prog_str))

        for i, item in enumerate(player.playlist, 1):
            if item.meta.get('channel', False) and item.meta.get('author', False):
                nextline = '`{}.` **{}** added by **{}**'.format(i, item.title, item.meta['author'].name).strip()
            else:
                nextline = '`{}.` **{}**'.format(i, item.title).strip()

            currentlinesum = sum(len(x) + 1 for x in lines)  # +1 is for newline char

            if currentlinesum + len(nextline) + len(andmoretext) > DISCORD_MSG_CHAR_LIMIT:
                if currentlinesum + len(andmoretext):
                    unlisted += 1
                    continue

            lines.append(nextline)

        if unlisted:
            lines.append('\n*... and %s more*' % unlisted)

        if not lines:
            lines.append(
                'no songs queued! Queue something with {}play.'.format(self.config.command_prefix))

        message = str('\n'.join(lines))
        em = discord.Embed(description=message, colour=(random.randint(0, 16777215)))
        em.set_author(name='Playlist:',
                      icon_url=self.user.avatar_url)
        try:
            await self.send_message(channel, embed=em)
        except:
            await self.safe_send_message(channel, message)

    async def cmd_pause(self, player, channel):
        """
        Usage:
            {command_prefix}pause
        Pauses playback of the current song.
        """

        if player.is_playing:
            player.pause()
            await self.send_message(channel, ":pause_button:")
        else:
            raise core.CommandError('Player is not playing.', expire_in=30)

    async def cmd_resume(self, player, channel):
        """
        Usage:
            {command_prefix}resume
        Resumes playback of a paused song.
        """

        if player.is_paused:
            player.resume()
            await self.send_message(channel, ":arrow_forward:")
        else:
            raise core.CommandError('Player is not paused.', expire_in=30)

    async def cmd_shuffle(self, channel, player):
        """
        Usage:
            {command_prefix}shuffle
        Shuffles the playlist.
        """

        player.playlist.shuffle()

        cards = [':white_circle:', ':black_circle:', ':red_circle:', ':large_blue_circle:']
        random.shuffle(cards)

        hand = await self.send_message(channel, ' '.join(cards))
        await asyncio.sleep(0.2)

        for x in range(5):
            random.shuffle(cards)
            await self.safe_edit_message(hand, ' '.join(cards))
            await asyncio.sleep(0.2)

        await self.edit_message(hand, ":ok:")

    async def cmd_clear(self, player, author):
        """
        Usage:
            {command_prefix}clear
        Clears the playlist.
        """

        player.playlist.clear()
        return Response('\N{PUT LITTER IN ITS PLACE SYMBOL}')

    async def cmd_getout(self, server, channel, author, message):
        if not author.voice_channel:
            return Response('You are not in a voice channel!')
        await self.send_typing(channel)
        dir = "data/settings/" + message.server.id + ".json"
        if not os.path.exists("data"):
            os.mkdir("data")
        if not os.path.exists("data/settings"):
            os.mkdir("data/settings")
        if not os.path.isfile(dir):
            prefix = self.config.command_prefix
        else:
            with open(dir, 'r') as r:
                data = json.load(r)
                prefix = str(data["prefix"])
        msg = await self.send_message(channel, ("Leaving...``" + author.voice_channel.name + "``"))
        await self.disconnect_voice_client(server)
        await self.edit_message(msg, ("Left ``" + author.voice_channel.name + "``"))
        await asyncio.sleep(3)
        await self.edit_message(msg, "Thanks for using Helix. If you like me, and want me to keep running, consider donating with ``{}donate``.".format(prefix))

    async def cmd_supported(self, channel):
        msg = "I use YoutubeDL to get the songs, if they support it, so do I:\n"
        msg += "https://rg3.github.io/youtube-dl/supportedsites.html"
        msg += " \n"
        msg += "I can also play live streams ^-^"
        em = discord.Embed(description=msg, colour=(random.randint(0, 16777215)))
        em.set_author(name='Supported Links:',
                      icon_url="http://images.clipartpanda.com/help-clipart-11971487051948962354zeratul_Help.svg.med.png")
        await self.send_message(channel, embed=em)

    async def cmd_loop(self, message, player, channel, author):
        await self.send_typing(channel)
        dir = "data/settings/" + message.server.id + ".json"
        if not os.path.exists("data"):
            os.mkdir("data")
        if not os.path.exists("data/settings"):
            os.mkdir("data/settings")
        if not os.path.isfile(dir):
            prefix = self.config.command_prefix
        else:
            with open(dir, 'r') as r:
                data = json.load(r)
                prefix = str(data["prefix"])
        message = message.content.strip()
        message = message.replace(prefix, "")
        message = message.replace("loop", "")
        message = message.replace(" ", "")
        try:
            count = int(message)
            if count > 100:
                return Response("Now why would i let you spam that much?")
            msg = "Looping song, " + str(count) + " times"
            await self.safe_send_message(channel, msg)
        except:
            await self.safe_send_message(channel,
                                         "You didn't specify how many times i should loop \n I'll assume you wanted 20 times")
            count = int(20)
        song_url = player.current_entry.url
        for i in range(count):
            await player.playlist.add_entry(song_url, channel=channel, author=author)
        return Response(":thumbsup:")

    async def cmd_add(self, channel, player, message, author):
        """
        Usage:
            {command_prefix}add http://pastebin.com/5upGeSzX

        Adds your urls from a pastebin paste. It will automatically skip any broken urls in your paste
        """
        try:
            message = message.content.strip()
            message = message[5:]
            link = code.misc.patebin(message)
            link = link.splitlines()
            if link == None:
                return Response("Please give me a pastebin url like this: **/add http://pastebin.com/5upGeSzX**")
        except:
            return Response("Please give me a pastebin url like this: **/add http://pastebin.com/5upGeSzX**")
        await self.safe_send_message(channel, "**IM PROCCESSING YOUR LINK HANG ON FAM**")
        count = int(0)
        for line in link:
            song_url = line
            print(line)
            info = await self.downloader.safe_extract_info(player.playlist.loop, song_url, download=False,
                                                           process=False)
            try:
                await player.playlist.add_entry(song_url, channel=channel, author=author)
                count = count + 1
            except Exception as e:
                print("Error adding song from autoplaylist:", e)
                msg = "Failed to add" + line
                await self.safe_send_message(channel, msg)
        count = str(count)
        msg = "Added " + count + " songs"
        return Response(msg)

    async def cmd_electronic(self, channel, player, author):
        msg = await self.send_message(channel, "Generating playlist...")
        size = 20
        url = self.playlisturl + "Electronic"
        async with aiohttp.get(url) as r:
            songlist = await r.text()
        songlist = songlist.splitlines()
        num = 0
        while num != size:
            song_url = (songlist[random.randint(0, ((len(songlist)) - 1))])
            if song_url not in player.playlist:
                info = await self.downloader.safe_extract_info(player.playlist.loop, song_url, download=False, process=False)
                try:
                    await player.playlist.add_entry(song_url, channel=channel, author=author)
                    num += 1
                    await self.edit_message(msg, ("Generating playlist... " + str(num) + "/" + str(size)))
                except:
                    pass
        await self.edit_message(msg, "Playlist ready")

    async def cmd_rock(self, channel, player, author):
        msg = await self.send_message(channel, "Generating playlist...")
        size = 20
        url = self.playlisturl + "Rock"
        async with aiohttp.get(url) as r:
            songlist = await r.text()
        songlist = songlist.splitlines()
        num = 0
        while num != size:
            song_url = (songlist[random.randint(0, ((len(songlist)) - 1))])
            if song_url not in player.playlist:
                info = await self.downloader.safe_extract_info(player.playlist.loop, song_url, download=False, process=False)
                try:
                    await player.playlist.add_entry(song_url, channel=channel, author=author)
                    num += 1
                    await self.edit_message(msg, ("Generating playlist... " + str(num) + "/" + str(size)))
                except:
                    pass
        await self.edit_message(msg, "Playlist ready")

    async def cmd_metal(self, channel, player, author):
        msg = await self.send_message(channel, "Generating playlist...")
        size = 20
        url = self.playlisturl + "Metal"
        async with aiohttp.get(url) as r:
            songlist = await r.text()
        songlist = songlist.splitlines()
        num = 0
        while num != size:
            song_url = (songlist[random.randint(0, ((len(songlist)) - 1))])
            if song_url not in player.playlist:
                info = await self.downloader.safe_extract_info(player.playlist.loop, song_url,
                                                               download=False, process=False)
                try:
                    await player.playlist.add_entry(song_url, channel=channel, author=author)
                    num += 1
                    await self.edit_message(msg, ("Generating playlist... " + str(num) + "/" + str(size)))
                except:
                    pass
        await self.edit_message(msg, "Playlist ready")

    async def cmd_retro(self, channel, player, author):
        msg = await self.send_message(channel, "Generating playlist...")
        size = 20
        url = self.playlisturl + "Retro"
        async with aiohttp.get(url) as r:
            songlist = await r.text()
        songlist = songlist.splitlines()
        num = 0
        while num != size:
            song_url = (songlist[random.randint(0, ((len(songlist)) - 1))])
            if song_url not in player.playlist:
                info = await self.downloader.safe_extract_info(player.playlist.loop, song_url,
                                                               download=False, process=False)
                try:
                    await player.playlist.add_entry(song_url, channel=channel, author=author)
                    num += 1
                    await self.edit_message(msg, ("Generating playlist... " + str(num) + "/" + str(size)))
                except:
                    pass
        await self.edit_message(msg, "Playlist ready")

    async def cmd_hiphop(self, channel, player, author):
        msg = await self.send_message(channel, "Generating playlist...")
        size = 20
        url = self.playlisturl + "Hiphop"
        async with aiohttp.get(url) as r:
            songlist = await r.text()
        songlist = songlist.splitlines()
        num = 0
        while num != size:
            song_url = (songlist[random.randint(0, ((len(songlist)) - 1))])
            if song_url not in player.playlist:
                info = await self.downloader.safe_extract_info(player.playlist.loop, song_url,
                                                               download=False, process=False)
                try:
                    await player.playlist.add_entry(song_url, channel=channel, author=author)
                    num += 1
                    await self.edit_message(msg,
                                            ("Generating playlist... " + str(num) + "/" + str(size)))
                except:
                    pass
        await self.edit_message(msg, "Playlist ready")

    async def cmd_jazz(self, channel, player, author):
        msg = await self.send_message(channel, "Generating playlist...")
        size = 20
        url = self.playlisturl + "Jazz"
        async with aiohttp.get(url) as r:
            songlist = await r.text()
        songlist = songlist.splitlines()
        num = 0
        while num != size:
            song_url = (songlist[random.randint(0, ((len(songlist)) - 1))])
            if song_url not in player.playlist:
                info = await self.downloader.safe_extract_info(player.playlist.loop, song_url,
                                                               download=False, process=False)
                try:
                    await player.playlist.add_entry(song_url, channel=channel, author=author)
                    num += 1
                    await self.edit_message(msg, ("Generating playlist... " + str(num) + "/" + str(size)))
                except:
                    pass
        await self.edit_message(msg, "Playlist ready")

    async def cmd_classical(self, channel, player, author):
        msg = await self.send_message(channel, "Generating playlist...")
        size = 20
        url = self.playlisturl + "Classical"
        async with aiohttp.get(url) as r:
            songlist = await r.text()
        songlist = songlist.splitlines()
        num = 0
        while num != size:
            song_url = (songlist[random.randint(0, ((len(songlist)) - 1))])
            if song_url not in player.playlist:
                info = await self.downloader.safe_extract_info(player.playlist.loop, song_url,
                                                               download=False, process=False)
                try:
                    await player.playlist.add_entry(song_url, channel=channel, author=author)
                    num += 1
                    await self.edit_message(msg,
                                            ("Generating playlist... " + str(num) + "/" + str(size)))
                except:
                    pass
        await self.edit_message(msg, "Playlist ready")

    async def cmd_japanese(self, channel, player, author):
        msg = await self.send_message(channel, "Generating playlist...")
        size = 20
        url = self.playlisturl + "Japanese"
        async with aiohttp.get(url) as r:
            songlist = await r.text()
        songlist = songlist.splitlines()
        num = 0
        while num != size:
            song_url = (songlist[random.randint(0, ((len(songlist)) - 1))])
            if song_url not in player.playlist:
                info = await self.downloader.safe_extract_info(player.playlist.loop, song_url,
                                                               download=False, process=False)
                try:
                    await player.playlist.add_entry(song_url, channel=channel, author=author)
                    num += 1
                    await self.edit_message(msg, ("Generating playlist... " + str(num) + "/" + str(size)))
                except:
                    pass
        await self.edit_message(msg, "Playlist ready")

    async def cmd_rap(self, channel, player, author):
        msg = await self.send_message(channel, "Generating playlist...")
        size = 20
        url = self.playlisturl + "Rap"
        async with aiohttp.get(url) as r:
            songlist = await r.text()
        songlist = songlist.splitlines()
        num = 0
        while num != size:
            song_url = (songlist[random.randint(0, ((len(songlist)) - 1))])
            if song_url not in player.playlist:
                info = await self.downloader.safe_extract_info(player.playlist.loop, song_url,
                                                               download=False, process=False)
                try:
                    await player.playlist.add_entry(song_url, channel=channel, author=author)
                    num += 1
                    await self.edit_message(msg,
                                            ("Generating playlist... " + str(num) + "/" + str(size)))
                except:
                    pass
        await self.edit_message(msg, "Playlist ready")

#
#
#
#events
    async def on_message(self, message):
        await self.wait_until_ready()
        if message.author.bot:
            return
        try:
            if message.server.id == "212751363463970816":
                if message.channel.id != "254342840585420810":
                    return
        except:
            log.warning("discord glitched out again")
        try:
            await self.rankcheck(message)
        except:
            pass
        author = message.author
        if message.channel.is_private:
            if message.author != self.user:
                log.info(("dm: " + message.author.name + "// " + message.content))
                return
        if author.id in self.blacklist:
            if author.id != self.config.owner_id:
                return
        message_content = message.content.strip()

        if self.user.mentioned_in(message):
            if len(message.content) == 21 or len(message_content) == 22:
                log.info("I was mentioned")
                await self.cmd_help(message.author, message.channel, message.server)

        if message.author != self.user:
            dir = "data/settings/" + message.server.id + ".json"
            if not os.path.exists("data"):
                os.mkdir("data")
            if not os.path.exists("data/settings"):
                os.mkdir("data/settings")
            if not os.path.isfile(dir):
                prefix = self.config.command_prefix
            else:
                with open(dir, 'r') as r:
                    data = json.load(r)
                    prefix = str(data["prefix"])
        else:
            prefix = "/"
        if not message_content.startswith(prefix):
            return
        if message.author == self.user:
            log.warning("Ignoring command from myself ({})".format(message.content))
            return
        command, *args = message_content.split(' ')
        command = command[len(prefix):].lower().strip()

        handler = getattr(self, 'cmd_' + command, None)
        if not handler:
            return

        if message.channel.is_private:
            if not (message.author.id == self.config.owner_id and command == 'joinserver'):
                return

        else:
            log.info("{0.id}/{0!s}: {1}".format(message.author, message_content.replace('\n', '\n... ')))

        user_permissions = self.permissions.for_user(message.author)

        argspec = inspect.signature(handler)
        params = argspec.parameters.copy()

        sentmsg = response = None

        # noinspection PyBroadException
        try:
            if user_permissions.ignore_non_voice and command in user_permissions.ignore_non_voice:
                passed = await self._check_ignore_non_voice(message)
            commands = "play spawn volume skip pause resume electronic rock metal japanese jazz metal retro classical hiphop remove"
            if command in commands and author.id != "174918559539920897":
                for role in author.roles:
                    role = str(role)
                    role = role.lower()
                    if role == "no_tunes":
                        await self.send_message(message.channel, "You have a role which disables music commands, sorry\n``role name: no_tunes``")
                        return
                    if not message.author.voice_channel:
                        return Response('You are not in a voice channel!')
            if command in commands:
                server = message.server
                self.server_specific_data[message.server]['last_np_msg'] = message
                print((self.server_specific_data[message.server]['last_np_msg']))
            handler_kwargs = {}
            if params.pop('message', None):
                handler_kwargs['message'] = message

            if params.pop('channel', None):
                handler_kwargs['channel'] = message.channel

            if params.pop('author', None):
                handler_kwargs['author'] = message.author

            if params.pop('server', None):
                handler_kwargs['server'] = message.server

            if params.pop('player', None):
                handler_kwargs['player'] = await self.get_player(message.channel)

            if params.pop('_player', None):
                handler_kwargs['_player'] = self.get_player_in(message.server)

            if params.pop('permissions', None):
                handler_kwargs['permissions'] = user_permissions

            if params.pop('user_mentions', None):
                handler_kwargs['user_mentions'] = list(map(message.server.get_member, message.raw_mentions))

            if params.pop('channel_mentions', None):
                handler_kwargs['channel_mentions'] = list(map(message.server.get_channel, message.raw_channel_mentions))

            if params.pop('voice_channel', None):
                handler_kwargs['voice_channel'] = message.server.me.voice_channel

            if params.pop('leftover_args', None):
                handler_kwargs['leftover_args'] = args

            args_expected = []
            for key, param in list(params.items()):

                # parse (*args) as a list of args
                if param.kind == param.VAR_POSITIONAL:
                    handler_kwargs[key] = args
                    params.pop(key)
                    continue

                # parse (*, args) as args rejoined as a string
                # multiple of these arguments will have the same value
                if param.kind == param.KEYWORD_ONLY and param.default == param.empty:
                    handler_kwargs[key] = ' '.join(args)
                    params.pop(key)
                    continue

                doc_key = '[{}={}]'.format(key, param.default) if param.default is not param.empty else key
                args_expected.append(doc_key)

                # Ignore keyword args with default values when the command had no arguments
                if not args and param.default is not param.empty:
                    params.pop(key)
                    continue

                # Assign given values to positional arguments
                if args:
                    arg_value = args.pop(0)
                    handler_kwargs[key] = arg_value
                    params.pop(key)

            if message.author.id != self.config.owner_id:
                if user_permissions.command_whitelist and command not in user_permissions.command_whitelist:
                    return Response(
                        "This command is not enabled for your group ({}).".format(user_permissions.name),
                        expire_in=20)

                elif user_permissions.command_blacklist and command in user_permissions.command_blacklist:
                    return Response(
                        "This command is disabled for your group ({}).".format(user_permissions.name),
                        expire_in=20)

            # Invalid usage, return docstring
            if params:
                docs = getattr(handler, '__doc__', None)
                if not docs:
                    docs = 'Usage: {}{} {}'.format(
                        self.config.command_prefix,
                        command,
                        ' '.join(args_expected)
                    )

                docs = dedent(docs)
                await self.safe_send_message(
                    message.channel,
                    '```\n{}\n```'.format(docs.format(command_prefix=self.config.command_prefix)),
                    expire_in=60
                )
                return
            try:
                response = await handler(**handler_kwargs)
            except:
                pass
            if response and isinstance(response, Response):
                content = response.content
                if response.reply:
                    content = '{}, {}'.format(message.author.mention, content)

                sentmsg = await self.safe_send_message(
                    message.channel, content,
                    expire_in=response.delete_after if self.config.delete_messages else 0,
                    also_delete=message if self.config.delete_invoking else None
                )

        except Exception as e:
            log.error("Error in {0}: {1.__class__.__name__}: {1.message}".format(command, e), exc_info=True)

            expirein = e.expire_in if self.config.delete_messages else None
            alsodelete = message if self.config.delete_invoking else None

            await self.safe_send_message(
                message.channel,
                '```\n{}\n```'.format(e.message),
                expire_in=expirein,
                also_delete=alsodelete
            )

        except exceptions.Signal:
            raise

        except Exception:
            log.error("Exception in on_message", exc_info=True)
            if self.config.debug_mode:
                await self.safe_send_message(message.channel, '```\n{}\n```'.format(traceback.format_exc()))

        finally:
            try:
                if not sentmsg and not response and self.config.delete_invoking:
                    await asyncio.sleep(5)
                    await self.safe_delete_message(message, quiet=True)
            except:
                pass

    async def on_voice_state_update(self, before, after):
        if not self.init_ok:
            return  # Ignore stuff before ready

        state = VoiceStateUpdate(before, after)

        if state.broken:
            log.voicedebug("Broken voice state update")
            return

        if state.resuming:
            log.debug("Resumed voice connection to {0.server.name}/{0.name}".format(state.voice_channel))

        if not state.changes:
            log.voicedebug("Empty voice state update, likely a session id change")
            return  # Session id change, pointless event

        ################################

        log.voicedebug("Voice state update for {mem.id}/{mem!s} on {ser.name}/{vch.name} -> {dif}".format(
            mem=state.member,
            ser=state.server,
            vch=state.voice_channel,
            dif=state.changes
        ))
        if not state.is_about_my_voice_channel:
            return  # Irrelevant channel

        if state.joining or state.leaving:
            log.info("{0.id}/{0!s} has {1} {2}/{3}".format(
                state.member,
                'joined' if state.joining else 'left',
                state.server,
                state.my_voice_channel
            ))

        if not self.config.auto_pause:
            return

        autopause_msg = "{state} in {channel.server.name}/{channel.name} {reason}"

        auto_paused = self.server_specific_data[after.server]['auto_paused']
        player = await self.get_player(state.my_voice_channel)

        if state.joining and state.empty() and player.is_playing:
            log.info(autopause_msg.format(
                state="Pausing",
                channel=state.my_voice_channel,
                reason="(joining empty channel)"
            ).strip())

            self.server_specific_data[after.server]['auto_paused'] = True
            player.pause()
            return

        if not state.is_about_me:
            if not state.empty(old_channel=state.leaving):
                if auto_paused and player.is_paused:
                    log.info(autopause_msg.format(
                        state="Unpausing",
                        channel=state.my_voice_channel,
                        reason=""
                    ).strip())

                    self.server_specific_data[after.server]['auto_paused'] = False
                    player.resume()
            else:
                if not auto_paused and player.is_playing:
                    log.info(autopause_msg.format(
                        state="Pausing",
                        channel=state.my_voice_channel,
                        reason="(empty channel)"
                    ).strip())

                    self.server_specific_data[after.server]['auto_paused'] = True
                    player.pause()

    async def on_server_update(self, before: discord.Server, after: discord.Server):
        if before.region != after.region:
            log.warning("Server \"%s\" changed regions: %s -> %s" % (after.name, before.region, after.region))

            await self.reconnect_voice_client(after)

    async def on_server_join(self, server: discord.Server):
        log.info("Bot has been joined server: {}".format(server.name))
        if server.id == "name":
            await self.leave_server(server)
            await self.safe_send_message(server,
                                         "This server has been blacklisted, if you feel this is a mistake, go argue your case in my server, https://discord.gg/WJG7a")
        if not self.user.bot:
            alertmsg = "<@{uid}> Hi I'm a Helix please mute me."

            if server.id == "81384788765712384" and not server.unavailable:  # Discord API
                playground = server.get_channel("94831883505905664") or discord.utils.get(server.channels,
                                                                                          name='playground') or server
                await self.safe_send_message(playground, alertmsg.format(uid="98295630480314368"))  # fake abal
                return
            elif server.id == "129489631539494912" and not server.unavailable:  # Rhino Bot Help
                bot_testing = server.get_channel("134771894292316160") or discord.utils.get(server.channels,
                                                                                            name='bot-testing') or server
                await self.safe_send_message(bot_testing, alertmsg.format(uid="98295630480314368"))  # also fake abal
                return
        msg = (
            "Hi there, Im Helix**2.0**. Type /help to see what i can do, and remember to join my server for news and updates: https://discord.gg/6K5JkF5 or follow my official twitter: https://twitter.com/helixbtofficial")
        msg = msg + "  Give me about 10 seconds to prepare some data for your server"
        em = discord.Embed(description=msg, colour=65280)
        em.set_author(name='I just joined :3',
                      icon_url=(self.user.avatar_url))
        try:
            await self.send_message(server, embed=em)
        except:
            await self.safe_send_message(server, msg)
        pathlib.Path('user/%s/' % server.id).mkdir(exist_ok=True)
        await self.dsync()
        await asyncio.sleep(8)
        await self.safe_send_message(server, "All done ^-^")

    async def on_server_remove(self, server: discord.Server):
        log.info("Bot has been removed from server: {}".format(server.name))
        log.debug('Updated server list:')
        [log.debug(' - ' + s.name) for s in self.servers]
        if server.id in self.players:
            self.players.pop(server.id).kill()
        await self.dsync()

    async def on_server_available(self, server: discord.Server):
        if not self.init_ok:
            return  # Ignore pre-ready events

        log.debug("Server \"{}\" has become available.".format(server.name))

        player = self.get_player_in(server)

        if player and player.is_paused:
            av_paused = self.server_specific_data[server]['availability_paused']

            if av_paused:
                log.debug("Resuming player in \"{}\" due to availability.".format(server.name))
                self.server_specific_data[server]['availability_paused'] = False
                player.resume()

    async def on_server_unavailable(self, server: discord.Server):
        log.debug("Server \"{}\" has become unavailable.".format(server.name))

        player = self.get_player_in(server)

        if player and player.is_playing:
            log.debug("Pausing player in \"{}\" due to unavailability.".format(server.name))
            self.server_specific_data[server]['availability_paused'] = True
            player.pause()
