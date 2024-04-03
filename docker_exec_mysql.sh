docker exec -i runner-1.8.7 mysql -u projectsystem -p${DOCKER_MYSQL_PASSWORD} database -A facile_building  <<< $1
