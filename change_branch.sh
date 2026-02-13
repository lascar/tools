#!/bin/bash

gitdiff=`git diff`
if [[ $gitdiff ]]
then
  echo "git diff not empty"
  exit 1
fi
read -r typo num reste <<< $(echo $1|perl -ane '/(?:#)?(t_)?([0-9]+)(_.*)/ and print"$1 $2 $3\n"')
echo "typo=$typo num=$num reste=$reste"
if [[ $num ]]
then
  if [[ $typo == "t_" ]]
  then
    branch="tickets/$1"
  else
    branch="feature/$1"
  fi
  echo "branch=$branch"
  git checkout master
  git pull
  git checkout $branch
  git merge master
  docker stop bofacile-web-1
  echo "on branch $branch"
else
  echo "on master?"
fi
