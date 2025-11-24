#!/bin/bash
[ ! -f .env ] || export $(grep -v '^#' .env | xargs)
docker restart project-shell-1 && docker  exec project-shell-1 mysqldump --no-data -u root -p${MYSQL_PASSWORD} -h database facile_building > doc/dump_facile_building_schema_`date '+%F'`.sql
