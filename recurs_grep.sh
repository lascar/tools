#!/bin/bash
if test -z "$2"
then
  dir="app/"
else
  dir=$2
fi
rgrep "$1" $dir 2>/dev/null |egrep -v '~:|^Bi|^grep'
