#!/bin/bash

doc=
if [[ $1 == "doc" ]]
then
  doc="_doc"
  doc/tools/desc_git.sh
  if [[ $2 ]]
  then
    branch=$2
    echo "branch : $branch"
    read -r typo num reste <<< $(echo $branch|perl -ane '/(?:#)?(t_)?([0-9]+)(_.*)/ and print"$1 $2 $3"')
    if [[ $reste == "" ]]
    then
      echo 'pas reste'
      read -r num reste <<< $(echo $branch|perl -ane '/(?:#)?([0-9]+)(_.*)/ and print"$1 $2 $3"')
      typo=
    fi
  else
    branch=$(git rev-parse --abbrev-ref HEAD)
    echo "branch : $branch"
    if [[ $branch == "master" ]]
    then
      echo "on est sur master"
      exit
    fi
    read -r typ num reste <<< $(echo $branch|perl -ane '/(?:#)?(feature|tickets)\/(?:#)?(?:t_)?([0-9]+)(_.*)/ and print"$1 $2 $3"')
    if [[ $typ == "tickets" ]]
    then
      typo="t_"
    fi
  fi
elif [[ $1 ]]
then
  branch=$1
  echo "branch : $branch"
  if [[ $branch == "master" ]]
  then
    echo "on est sur master"
    exit
  fi
  read -r typo num reste <<< $(echo $branch|perl -ane '/(?:#)?(t_)?([0-9]+)(_.*)/ and print"$1 $2 $3"')
  if [[ $reste == "" ]]
  then
    echo 'pas reste'
    read -r num reste <<< $(echo $branch|perl -ane '/(?:#)?([0-9]+)(_.*)/ and print"$1 $2 $3"')
    typo=
  fi
else
  branch=$(git rev-parse --abbrev-ref HEAD)
  if [[ $branch == "master" ]]
  then
    echo "on est sur master"
    exit
  fi
  echo "branch : $branch"
  read -r typ num reste <<< $(echo $branch|perl -ane '/(?:#)?(feature|tickets)\/(?:#)?(?:t_)?([0-9]+)(_.*)/ and print"$1 $2 $3"')
  if [[ $typ == "tickets" ]]
  then
    typo="t_"
  fi
fi
echo "typ : $typ"
echo "typo : $typo"
echo "num : $num"
echo "reste : $reste"

vim -S vimsession_$typo$num$reste$doc
