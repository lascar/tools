docker exec -i runner-1.8.7 mysql -u bofacilesystem -p${DOCKER_MYSQL_PASSWORD} efsql1-lan -A facile_building  <<< $1
