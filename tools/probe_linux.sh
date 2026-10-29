#!/usr/bin/env bash
# Read-only probe of the Linux side. Contains no secrets.
echo "--- os ---"
head -3 /etc/os-release 2>/dev/null
uname -r
echo "pid1=$(cat /proc/1/comm 2>/dev/null)"
if [ -f /.dockerenv ]; then echo "DOCKER=yes"; else echo "DOCKER=no"; fi

echo "--- home ---"
echo "HOME=$HOME"
echo "whoami=$(whoami)"
ls -la "$HOME" | head -25
df -h "$HOME" | tail -1

echo "--- python ---"
python3 --version
ls /usr/bin/python3* 2>/dev/null
python3 -c 'import sys;print("exe="+sys.executable)'

echo "--- tools ---"
for t in crontab sshd rsync ssh sftp tmux screen nohup curl wget unzip tar gzip base64 autossh sshfs fusermount python3; do
  printf "%-10s " "$t"
  command -v "$t" || echo MISSING
done

echo "--- env ---"
echo "PATH=$PATH"
echo "SHELL=$SHELL"

echo "--- sshdir ---"
ls -la "$HOME/.ssh" 2>/dev/null || echo "no .ssh"

echo "--- linger ---"
loginctl show-user "$(whoami)" -p Linger 2>&1

echo "--- sshd effective ---"
sshd -T 2>/dev/null | grep -Ei '^(allowtcpforwarding|gatewayports|permittunnel|clientaliveinterval|clientalivecountmax|pubkeyauthentication|passwordauthentication|maxsessions) ' || echo "sshd -T not permitted (normal for non-root)"

echo "--- listen 22/fwd ---"
ss -tln 2>/dev/null | grep -E ':(22|18080) ' || netstat -tln 2>/dev/null | grep -E ':(22|18080) '

echo "--- cron service ---"
systemctl is-active cron 2>/dev/null || echo "systemctl cron unknown"
ls /etc/cron.d 2>/dev/null | head -5

echo "--- disk quota sanity ---"
df -h /tmp | tail -1
