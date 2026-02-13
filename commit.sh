#!/bin/bash

branch=$(git rev-parse --abbrev-ref HEAD)
echo "branch : $branch"
read -r typ num reste <<< $(echo $branch|perl -ane '/(?:#)?(feature|tickets)\/(?:#)?(?:t_)?([0-9]+)(_.*)/ and print"$1 $2 $3"')
if [[ $branch != "master" ]]
then
  if [[ $typ == "tickets" ]]
  then
    typo="t_"
  fi
  files_to_commit=$(git status|perl -ane '(/modified|renamed|deleted/ and print "$F[1] ") or (/new file/ and print "$F[2] ")')
  echo "files_to_commit : $files_to_commit"
  message="$typo$num$reste: $1"
  echo "message : $message"
  if [[ $2 == 'nv' ]]
  then
    git commit --no-verify $(echo $files_to_commit) -m "$message"
  else
    git commit  $(echo $files_to_commit) -m "$message"
  fi
  else
  echo "pas de commit sur master"
fi
