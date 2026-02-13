#!/bin/bash

branch=$(git rev-parse --abbrev-ref HEAD|perl -ane 's/(tickets|feature)\///; print')
if [[ $branch == "master" ]]
then
  echo "branch master"
else
  read -r typo num reste <<< $(echo $branch|perl -ane '/(?:#)?(t_)?([0-9]+)(_.*)/ and print"$1 $2 $3"')
  patch_begin_name="doc/patch_$typo$num$reste"
fi
if [[ $1 != "done" ]]
  then
    termin=.txt
fi
ls -ltr ${patch_begin_name}*$termin
# patch auggie
patch_begin_name="doc/patch_auggie_$typo$num$reste"
echo "+ patch auggie"
ls -ltr ${patch_begin_name}*$termin 2>/dev/null
