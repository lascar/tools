#!/bin/bash
[ ! -f .env ] || export $(grep -v '^#' .env | xargs)
docker restart bofacile-shell-1 && docker  exec bofacile-shell-1 mysqldump --no-data -u root -p#{MYSQL_ROOT_PASSWORD} -h efsql1-lan facile_building > doc/dump_facile_building_schema_`date '+%F'`.sql
