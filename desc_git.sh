#!/bin/bash

if [[ $1  ]]
then
  branch=$1
  echo "branch : $branch"
  read -r typo num reste <<< $(echo $branch|perl -ane '/(?:#)?(t_)?([0-9]+)(_.*)?/ and print"$1 $2 $3"')
  if [[ $reste == "" ]]
  then
    echo 'pas reste'
    read -r num reste <<< $(echo $branch|perl -ane '/(?:#)?([0-9]+)(_.*)?/ and print"$1 $2 $3"')
    typo=
  fi
else
  branch=$(git rev-parse --abbrev-ref HEAD)
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
echo "file : doc/${typo}${num}${reste}.txt"
hashes=$(git log  --grep="$num"|perl -ane '/^commit/ and print "$F[1] "')
if [[ "${hashes}" != "" ]]
then
  echo -e "$hashes"|while read a; do git show $a; done> doc/tmp.txt
  files=$(cat doc/tmp.txt |perl -ane '/\+\+\+ b/ and /\+\+\+ b\/(.*)/ and print "$1\n"' | sort | uniq)
  echo -e "$files\n" > doc/${num}${reste}.txt
  cat doc/tmp.txt >> doc/$typo${num}${reste}.txt
else
  echo "rien dans le git log" > doc/${num}${reste}.txt
fi
