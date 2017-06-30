from __future__ import print_function
from time import gmtime, strftime
import os
import sys
import time
import logging
import tempfile
import colorlog
import traceback
import subprocess
import discord

tmpfile = tempfile.TemporaryFile('w+', encoding='utf8')
log = logging.getLogger('launcher')
log.setLevel(logging.DEBUG)

sh = logging.StreamHandler(stream=sys.stdout)
sh.setFormatter(logging.Formatter(
    fmt="[%(levelname)s] %(name)s: %(message)s"
))

sh.setLevel(logging.INFO)
log.addHandler(sh)

tfh = logging.StreamHandler(stream=tmpfile)
tfh.setFormatter(logging.Formatter(
    fmt="[%(levelname)s] %(name)s: %(message)s"
))
tfh.setLevel(logging.DEBUG)
log.addHandler(tfh)


def finalize_logging():
    pass

    with open("logs/bot.log", 'w', encoding='utf8') as f:
        tmpfile.seek(0)
        f.write(tmpfile.read())
        tmpfile.close()

        f.write('\n')
        f.write(" PRE-RUN SANITY CHECKS PASSED ".center(80, '#'))
        f.write('\n\n\n')

    global tfh
    log.removeHandler(tfh)
    del tfh

    fh = logging.FileHandler("logs/bot.log", mode='a')
    fh.setFormatter(logging.Formatter(
        fmt="[%(levelname)s]: %(message)s"
    ))
    fh.setLevel(logging.DEBUG)
    log.addHandler(fh)

    sh.setLevel(logging.INFO)

    dlog = logging.getLogger('discord')
    dlog.setLevel(logging.WARNING)
    dlh = logging.StreamHandler(stream=sys.stdout)
    dlh.terminator = ''
    dlh.setFormatter(logging.Formatter('.'))
    dlog.addHandler(dlh)


def pyexec(pycom, *args, pycom2=None):
    pycom2 = pycom2 or pycom
    os.execlp(pycom, pycom2, *args)


def restart(*args):
    pyexec(sys.executable, *args, *sys.argv, pycom2='python')


def main():
    if not os.path.exists("data"):
        log.info("Creating data folder")
        os.mkdir("data")
    if not os.path.isfile("data/options.ini"):
        log.critical("options.ini doesnt exist")
        exit()
    if not os.path.isfile("data/permissions.ini"):
        log.critical("permissions.ini doesnt exist")
        exit()
    if not os.path.isfile("data/blacklist.txt"):
        log.warning("blacklist.txt doesnt exist")
        target = open("data/blacklist.txt", "w")
        target.writelines(" ")
        target.close()
    if not os.path.exists("logs"):
        log.info("Creating logging folder")
        os.mkdir("logs")
    if not os.path.exists("code"):
        log.critical("THERE IS NO CODE!!!")
        os.mkdir("code")
        log.info("Created code folder, shutting down")
        exit()
    if not os.path.isfile("code/bot.py"):
        log.critical("THE BOT SCRIPT IS GONE!!!")
        log.critical("ABORTING BOOT")
        exit()
    if not os.path.isfile("logmein.py"):
        log.warning("The token script isnt here. HOW DO YOU EXPECT ME TO LOGIN???")
        code = """def token():
    token = "[put your token here]"
    return token"""
        target = open("logmein.py", "w")
        target.writelines(code)
        target.close()
        log.warning("There, ive made it for you. Slap the token in it, then boot me back up")
        exit()
    if not os.path.exists("user"):
        log.warning("User folder doesnt exist")
        log.warning("creating user folder")
        os.mkdir("user")
    if not os.path.isfile("level_blck.txt"):
        log.info("level blacklisting file doesnt exist")
        target = open("level_blck.txt", "w")
        target.writelines("placeholder ")
        target.close()
    else:
        try:
            import logmein
        except SyntaxError or ImportError:
            log.critical("something is wrong with logmein.py, you should fix that")
            exit()
        token = logmein.token()
        if token == "[put your token here]":
            log.critical("YOU DUMB FUCK, HOW DO YOU EXPECT ME TO LOG IN IF YOU DONT GIVE ME A VALID TOKEN")
            exit()
    log.info("All the files are there, well done genius. Now lets import them and get memin on Discord")
    finalize_logging()
    tryagain = True

    while tryagain:
        h = None
        from code import Helix
        h = Helix()

        log.info("Connecting\n")

        h.run()
        log.info("All done.")

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log.fatal("Bot runtime has been terminated")
        log.fatal(e)
        os.execl(sys.executable, sys.executable, *sys.argv)