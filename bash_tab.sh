#!/bin/bash

cd ~/development/project
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
gnome-terminal --tab --title "console" -- bash -c "docker attach project-console-1; echo -e 'docker start project-console-1 && docker attach project-console-1\ndetach ctrl-p ctrl-q\n'; exec bash -i"
gnome-terminal --tab --title "server" -- bash -c "docker restart project-web-1 && docker attach project-web-1; echo 'docker restart project-web-1 && docker attach project-web-1'; exec bash -i"
gnome-terminal --tab --title "vimsession" -- bash -c "doc/tools/vim_session.sh; exec bash -i"
gnome-terminal --tab --title "vimsession_doc" -- bash -c "doc/tools/vim_session.sh doc; exec bash -i"
cd ~/development/project
