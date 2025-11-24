#!/bin/bash
[ ! -f .env ] || export $(grep -v '^#' .env | xargs)
docker exec -i runner-1.8.7 mysql -u projectsystem -p${MYSQL_PASSWORD} database -A facile_building  <<< $1
