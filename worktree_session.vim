" Reprendre aussi les variables d’un superviseur antérieur, selon .env.
let s:worktree_config = fnamemodify(resolve(expand('<sfile>:p')), ':h') . '/_wt_config.py'
for s:worktree_key in ['VIM_SESSION', 'VIM_CLOSE', 'VIM_REFUSED']
  if empty(getenv('WT_' . s:worktree_key))
    let s:worktree_value = system('python3 ' . shellescape(s:worktree_config) . ' --session-env ' . s:worktree_key)
    if v:shell_error == 0
      call setenv('WT_' . s:worktree_key, substitute(s:worktree_value, '\n$', '', ''))
    else
      echoerr 'Configuration worktree inaccessible'
    endif
  endif
endfor
" Fermeture coopérative : ne jamais jeter un buffer modifié.
function! WorktreeCloseCheck(timer) abort
  if !filereadable($WT_VIM_CLOSE)
    return
  endif
  call delete($WT_VIM_CLOSE)
  if !empty(getbufinfo({'bufmodified': 1}))
    call writefile(['Buffers modifiés : enregistrer avec :wall puis relancer worktree_close.sh.'], $WT_VIM_REFUSED)
    echohl WarningMsg | echom 'Atelier : enregistre les buffers avant la fermeture.' | echohl None
    return
  endif
  try
    execute 'mksession! ' . fnameescape($WT_VIM_SESSION)
    qall
  catch
    call writefile([v:exception], $WT_VIM_REFUSED)
  endtry
endfunction
if has('timers')
  call timer_start(300, function('WorktreeCloseCheck'), {'repeat': -1})
endif
