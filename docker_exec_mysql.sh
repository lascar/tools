#!/bin/bash
[ ! -f .env ] || export $(grep -v '^#' .env | xargs)
docker exec -i runner-1.8.7 mysql -u bofacilesystem -p${MYSQL_PASSWORD} efsql1-lan -A facile_building  <<< $1
