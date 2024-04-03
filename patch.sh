#!/bin/bash

branch=$(git rev-parse --abbrev-ref HEAD)
echo "branch : $branch"
read -r typ num reste <<< $(echo $branch|perl -ane '/(?:#)?(feature|tickets)\/(?:#)?(?:t_)?([0-9]+)(_.*)/ and print"$1 $2 $3"')
typo=""
if [[ $typ == "tickets" ]]
then
  typo="t_"
fi
echo "typ : $typ"
echo "typo : $typo"
echo "num : $num"
echo "reste : $reste"
current_day=$(date +'%d%m%y')
file="doc/patch_${typo}${num}${reste}_${current_day}.txt"
echo "file : $file"

if [[ $1 == "make" ]]
then
  ls $file  >/dev/null 2>&1
  if [[ $? == 0 ]]
  then
    echo "patch déja crée"
    echo $file
  else
    git diff > $file
  fi
else
  ls $file  >/dev/null 2>&1
  if [[ $? == 0 ]]
  then
    git merge master
    patch -p1 < $file
    mv $file ${file}.done
  else
    echo "no patch found"
  fi
fi
