#!/bin/bash

branch_name=$1

git checkout master
git pull

if [ -z "$branch_name" ]; then
  echo "Usage: $0 <branch_name>"
  exit 1
fi

# Check if the branch already exists
if git show-ref --verify --quiet refs/heads/$branch_name; then
  echo "Branch $branch_name already exists. Exiting..."
  exit 1
fi

# Create a new branch from the current branch
git checkout -b feature/$branch_name
touch doc/${branch_name}.txt
touch doc/${branch_name}_suivi.txt
vim doc/${branch_name}.txt doc/${branch_name}_suivi.txt
