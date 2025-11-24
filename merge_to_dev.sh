#!/bin/bash

branch=$(git rev-parse --abbrev-ref HEAD)
echo "branch : $branch"
git checkout master
git pull
days_ago=${1:-1}
after_date=$(date +%Y-%m-%d -d "$days_ago day ago")

# Récupérer les commits de pcarrie@incwo sur master (non-merges)
commit_hashes_master=($(git log master --oneline --author pcarrie@incwo --no-merges --after=$after_date|perl -ane 'print "$F[0] "'))

# Récupérer les commits de merge sur master
commit_merge=($(git log master --oneline --merges --after=$after_date|perl -ane 'print "$F[0] "'))

echo "mergés sur master"
for value in "${commit_hashes_master[@]}"
do
    if [[ ! " ${commit_merge[*]} " =~ " ${value} " ]]; then
        echo $value
        git log --format="%ci %B" -n 1 $value
    fi
done

echo ""

# Récupérer tous les commits de pcarrie@incwo sur toutes les branches (non-merges)
commit_hashes_all=($(git log --all --oneline --author pcarrie@incwo --no-merges --after=$after_date|perl -ane 'print "$F[0] "'))

# Récupérer tous les commits de pcarrie@incwo qui sont dans master
commit_hashes_in_master=($(git log master --oneline --author pcarrie@incwo --no-merges --after=$after_date|perl -ane 'print "$F[0] "'))

echo "pas mergés sur master"
for value in "${commit_hashes_all[@]}"
do
    if [[ ! " ${commit_hashes_in_master[*]} " =~ " ${value} " ]]; then
        echo $value
        git log|grep $value || git log --format="%ci %B" -n 1 $value
    fi
done
git checkout $branch
