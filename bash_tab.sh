#!/bin/bash

cd ~/development/bo.facile
branch=$(git rev-parse --abbrev-ref HEAD)
if [ "$branch" != "master" ]; then
  doc/tools/patch.sh make
  git checkout .
  git checkout master
  git pull
  git checkout $branch
  doc/tools/patch.sh
fi
gnome-terminal --tab --title "docker compose" -- bash -c "docker compose -f docker-compose.build.187.yml up; exec bash -i"
sleep 4
gnome-terminal --tab --title "console" -- bash -c "docker attach bofacile-console-1; echo -e 'docker start bofacile-console-1 && docker attach bofacile-console-1\ndetach ctrl-p ctrl-q\n'; exec bash -i"
gnome-terminal --tab --title "server" -- bash -c "docker restart bofacile-web-1 && docker attach bofacile-web-1; echo 'docker restart bofacile-web-1 && docker attach bofacile-web-1'; exec bash -i"
gnome-terminal --tab --title "vimsession" -- bash -c "doc/tools/vim_session.sh; exec bash -i"
gnome-terminal --tab --title "vimsession_doc" -- bash -c "doc/tools/vim_session.sh doc; exec bash -i"
cd ~/development/bo.facile
