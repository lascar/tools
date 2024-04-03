#!/bin/bash
docker exec -t runner-1.8.7 mysql -u bofacilesystem -p${DOCKER_MYSQL_PASSWORD} -h efsql1-lan facile_building -e 'show tables;'| perl -ane '!/Tables/ and !/\+/ and !/Warning/ and print "$F[1]\n"' > doc/liste_tables.txt
cat doc/liste_tables.txt |while read a; do echo "######################################"; echo $a; echo;doc/docker_exec_mysql.sh "desc $a"|perl -ane '!/Field/ and print'; echo;done> doc/desc_tables.txt
