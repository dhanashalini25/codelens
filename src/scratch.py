import subprocess

API_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"


def run_it(command, seen=[]):
    seen.append(command)
    try:
        return subprocess.run(command, shell=True)
    except:
        pass