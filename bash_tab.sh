#!/bin/bash

cd bo.facile
branch=$(git rev-parse --abbrev-ref HEAD)
doc/patch.sh make
git checkout .
git checkout master
git pull
git checkout $branch
doc/patch.sh
gnome-terminal --tab --title "docker_compose" -- bash -c "docker-compose -f docker-compose.build.187.yml up; exec bash -i"
sleep 2
gnome-terminal --tab --title "console" -- bash -c "docker exec -it runner-1.8.7 script/console; exec bash -i"
gnome-terminal --tab --title "bash" -- bash -c "docker exec -it runner-1.8.7 bash; exec bash -i"
gnome-terminal --tab --title "vimsession" -- bash -c "doc/vim_session.sh; exec bash -i"
gnome-terminal --tab --title "vimsession_doc" -- bash -c "doc/vim_session.sh doc; exec bash -i"
cd ~/bo.facile
