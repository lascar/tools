#!/bin/bash

branch=$(git rev-parse --abbrev-ref HEAD)
if [[ $1 == "develop" ]]
then
  knock -d 500 -v dev-b.incwo.com 41822 5489 18963
  git push --force $1 $branch
else
  git push origin $branch
fi
