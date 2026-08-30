; Install-time and uninstall-time extras.
;
; Picked up automatically: electron-builder looks for `installer.nsh` in
; buildResources (`electron/build`), so this needs no entry in package.json.
; It is included at the very top of the generated script, before MUI2 and
; before the page list, which is why `customPageAfterChangeDir` below can
; add a page at all.
;
; TWO jobs live here.
;
; 1. The agent surface (O-9). The product ships a CLI -- `mfp.exe`, beside
;    the sidecar -- and a Skill that teaches an AI agent to call it. Neither
;    is any use if the agent cannot find the command, and there is no
;    cross-vendor standard for a locally installed CLI to announce itself:
;    Claude Code reads `%USERPROFILE%\.claude\skills`, Codex reads
;    `~/.codex/AGENTS.md`, a repository's own `AGENTS.md` is read by many
;    tools but only inside that repository. So the page below offers the one
;    mechanism that is not vendor-specific (PATH) and, separately, the one
;    that is (a skills file), each as its own tick-box, each undone on
;    uninstall. Neither is done silently and neither is done by default
;    without a reason -- see `mfpAgentPageCreate` for what decides the
;    initial state of the second box.
;
;    The work itself is done by `mfp.exe install-path` and
;    `mfp.exe agent-register`, not by NSIS. The PATH edit is the dangerous
;    one -- `ReadRegStr` is bounded by the build's NSIS_MAX_STRLEN and a
;    truncated read written back is how an installer eats somebody's PATH --
;    and in Python it is a pure function with tests around it.
;
; 2. Uninstall-time cleanup. `deleteAppDataOnUninstall` is documented in the
;    installed electron-builder schema (26.15.3) as "one-click installer
;    only", and this is an assisted installer (`oneClick: false`). The flag
;    is inert here, so the choice has to be made in the uninstaller itself.
;
;    Everything this app writes outside its install directory lives in ONE
;    folder -- `main.ts` points Electron's userData at the same
;    `%APPDATA%\media-fetch-pipeline` the Python side already uses, and the
;    Chrome profile defaults to `chrome-profile` inside it (doctor.py, PSM
;    §4.6). Measured on a real install: 295.8 MB, of which 285.2 MB is the
;    browser profile. Leaving that behind is exactly the thing the user
;    asked not to happen.
;
;    What is NOT touched, deliberately:
;
;      the output folder   Downloaded media is the user's data, not the
;                          app's. INV-6 already says removing a RECORD never
;                          deletes files; an uninstaller that took the
;                          downloads would be the same promise broken one
;                          level up.
;      %TEMP%\_MEI*        PyInstaller onefile extraction dirs. They ARE
;                          ours (measured: created per launch, cleaned on a
;                          graceful exit, orphaned by taskkill /F), but the
;                          name is generic -- every onefile program on the
;                          machine produces one, so sweeping them would
;                          delete other programs' state. The fix belongs at
;                          the source, not here.
;
; Silent runs do neither job. A silent uninstall is what an in-place update
; performs: prompting there would hang an unattended process, and undoing
; the PATH there would take it away on every update, since the silent
; install that follows has no page to re-tick.

!include nsDialogs.nsh
!include LogicLib.nsh

; File scope, but installer-only. The uninstaller is compiled from this same
; script with BUILD_UNINSTALLER defined, and that pass includes neither the
; page nor the install section -- so every one of these would be unreferenced
; there, and makensis runs with warnings as errors. The guard is also a check
; on that claim: if `customInstall` were compiled into the uninstaller after
; all, the build would fail loudly on an unknown variable rather than
; quietly doing the wrong thing.
!ifndef BUILD_UNINSTALLER
  Var mfpAddPath
  Var mfpAddSkill
  Var mfpSkillDir
  Var mfpPathCheckbox
  Var mfpSkillCheckbox
  Var mfpSkillField
  Var mfpBrowseButton
!endif

; Where this build's CLI ends up. `extraResources` puts the PyInstaller
; onedir tree at `resources\sidecar`, and `mfp.exe` needs `_internal` beside
; it, so this directory -- not $INSTDIR -- is what goes on PATH.
!define MFP_CLI_DIR "$INSTDIR\resources\sidecar"
!define MFP_CLI "${MFP_CLI_DIR}\mfp.exe"

; Our own record of what was done, so the uninstaller undoes exactly that
; and nothing else. Per-user by nature: the PATH edited is HKCU's and the
; skills directory is inside the profile.
!define MFP_STATE_KEY "Software\media-fetch-pipeline"

!macro customPageAfterChangeDir
  Function mfpSyncSkillControls
    ${NSD_GetState} $mfpSkillCheckbox $R0
    ${If} $R0 == 1
      EnableWindow $mfpSkillField 1
      EnableWindow $mfpBrowseButton 1
    ${Else}
      EnableWindow $mfpSkillField 0
      EnableWindow $mfpBrowseButton 0
    ${EndIf}
  FunctionEnd

  Function mfpSkillToggled
    Pop $R0 ; the control that was clicked
    Call mfpSyncSkillControls
  FunctionEnd

  Function mfpBrowseClicked
    Pop $R0 ; the control that was clicked
    ${NSD_GetText} $mfpSkillField $R1
    nsDialogs::SelectFolderDialog "選擇要放技能說明檔的資料夾" "$R1"
    Pop $R2
    ${If} $R2 != error
      ${NSD_SetText} $mfpSkillField "$R2"
    ${EndIf}
  FunctionEnd

  Function mfpAgentPageCreate
    ; First time through only, so going Back and Next again keeps whatever
    ; the user chose rather than resetting it under them.
    ${If} $mfpSkillDir == ""
      StrCpy $mfpSkillDir "$PROFILE\.claude\skills\media-fetch-pipeline"
      ; PATH defaults ON: it is reversible, it is undone on uninstall, and
      ; it is the only thing here that helps an agent this installer has
      ; never heard of.
      StrCpy $mfpAddPath "1"
      ; The skills file defaults ON only when there is EVIDENCE the user
      ; runs that agent -- writing into another program's configuration on
      ; a machine that does not have it is presumptuous, and an empty
      ; `.claude` tree is a good enough signal to ask for.
      ${If} ${FileExists} "$PROFILE\.claude\skills\*.*"
        StrCpy $mfpAddSkill "1"
      ${Else}
        StrCpy $mfpAddSkill "0"
      ${EndIf}
    ${EndIf}

    !insertmacro MUI_HEADER_TEXT "AI Agent 支援" "讓 AI 代理程式找得到、也知道怎麼呼叫這個工具。兩項都可以略過，之後也能反悔。"

    nsDialogs::Create 1018
    Pop $R9
    ${If} $R9 == error
      Abort
    ${EndIf}

    ${NSD_CreateLabel} 0 0 100% 24u "安裝資料夾裡會多一個命令列工具 mfp.exe。AI 代理程式（Claude Code、Codex 等）可以直接呼叫它下載貼文媒體，不必自己寫爬蟲，也不會繞過這個工具的速率保護。"
    Pop $R0

    ${NSD_CreateCheckbox} 0 30u 100% 11u "把工具資料夾加入我的 PATH（建議）"
    Pop $mfpPathCheckbox
    ${NSD_SetState} $mfpPathCheckbox $mfpAddPath

    ${NSD_CreateLabel} 10u 43u 96% 18u "這是唯一不綁特定 AI 工具的做法：任何能執行指令的代理程式都能用 where mfp 找到它，再用 mfp agent-guide 讀完整的呼叫規則。解除安裝時會一併還原。"
    Pop $R0

    ${NSD_CreateCheckbox} 0 64u 100% 11u "同時安裝技能說明檔（SKILL.md）到："
    Pop $mfpSkillCheckbox
    ${NSD_SetState} $mfpSkillCheckbox $mfpAddSkill
    ${NSD_OnClick} $mfpSkillCheckbox mfpSkillToggled

    ${NSD_CreateText} 10u 77u 74% 12u "$mfpSkillDir"
    Pop $mfpSkillField

    ${NSD_CreateButton} 86% 77u 14% 12u "瀏覽…"
    Pop $mfpBrowseButton
    ${NSD_OnClick} $mfpBrowseButton mfpBrowseClicked

    ${NSD_CreateLabel} 10u 92u 96% 26u "預設位置是 Claude Code 讀取個人技能的資料夾。其他代理程式沒有技能機制，只有一份全域指示檔（Codex 是 ~/.codex/AGENTS.md），寫進去的是一小段指標；裝好後執行 mfp agent-register --help。不勾選也不影響工具本身能用。"
    Pop $R0

    Call mfpSyncSkillControls
    nsDialogs::Show
  FunctionEnd

  Function mfpAgentPageLeave
    ${NSD_GetState} $mfpPathCheckbox $mfpAddPath
    ${NSD_GetState} $mfpSkillCheckbox $mfpAddSkill
    ${NSD_GetText} $mfpSkillField $mfpSkillDir
  FunctionEnd

  Page custom mfpAgentPageCreate mfpAgentPageLeave
!macroend

!macro customInstall
  ; `InstallLocation` was absent from the Add/Remove entry -- everything
  ; else was there (DisplayName, DisplayIcon, EstimatedSize,
  ; UninstallString), but tooling that asks "where is it installed" got
  ; nothing back. SHCTX is the hive the installer already chose from
  ; `perMachine`, so this lands in the same key electron-builder wrote the
  ; rest into.
  WriteRegStr SHCTX "${UNINSTALL_REGISTRY_KEY}" "InstallLocation" "$INSTDIR"

  ${IfNot} ${Silent}
    ${If} $mfpAddPath == 1
      DetailPrint "將 ${MFP_CLI_DIR} 加入使用者 PATH…"
      nsExec::ExecToLog '"${MFP_CLI}" install-path'
      Pop $0
      ${If} $0 == 0
        ; Recorded only on success, so the uninstaller never tries to undo
        ; something that did not happen.
        WriteRegStr HKCU "${MFP_STATE_KEY}" "AgentPathDir" "${MFP_CLI_DIR}"
      ${Else}
        DetailPrint "PATH 未變更（mfp install-path 回傳 $0）"
      ${EndIf}
    ${EndIf}

    ${If} $mfpAddSkill == 1
    ${AndIf} $mfpSkillDir != ""
      DetailPrint "安裝技能說明檔到 $mfpSkillDir …"
      nsExec::ExecToLog '"${MFP_CLI}" agent-register --target claude --path "$mfpSkillDir"'
      Pop $0
      ${If} $0 == 0
        WriteRegStr HKCU "${MFP_STATE_KEY}" "AgentSkillDir" "$mfpSkillDir"
      ${Else}
        DetailPrint "技能說明檔未安裝（mfp agent-register 回傳 $0）"
      ${EndIf}
    ${EndIf}
  ${EndIf}
!macroend

!macro customUnInstall
  ; Runs BEFORE the installed files are deleted, which is what lets these
  ; two commands run at all.
  ${IfNot} ${Silent}
    ReadRegStr $0 HKCU "${MFP_STATE_KEY}" "AgentPathDir"
    ${If} $0 != ""
      DetailPrint "從使用者 PATH 移除 $0 …"
      nsExec::ExecToLog '"${MFP_CLI}" install-path --remove --dir "$0"'
      Pop $1
      DeleteRegValue HKCU "${MFP_STATE_KEY}" "AgentPathDir"
    ${EndIf}

    ReadRegStr $0 HKCU "${MFP_STATE_KEY}" "AgentSkillDir"
    ${If} $0 != ""
      DetailPrint "移除技能說明檔 $0 …"
      nsExec::ExecToLog '"${MFP_CLI}" agent-register --target claude --path "$0" --remove'
      Pop $1
      DeleteRegValue HKCU "${MFP_STATE_KEY}" "AgentSkillDir"
    ${EndIf}

    DeleteRegKey /ifempty HKCU "${MFP_STATE_KEY}"
  ${EndIf}

  ; A silent uninstall is what an in-place update runs. Prompting there
  ; would hang an unattended process, and deleting there would take the
  ; queue with an upgrade -- so silent always keeps.
  IfSilent mfp_keep_data 0

  MessageBox MB_YESNO|MB_ICONQUESTION \
    "要一併刪除設定、下載佇列與瀏覽器設定檔嗎？$\r$\n$\r$\n\
$APPDATA\media-fetch-pipeline$\r$\n$\r$\n\
已經下載完成的檔案存放在你設定的輸出資料夾，不在這裡，不會被刪除。" \
    /SD IDNO IDYES mfp_delete_data

  Goto mfp_keep_data

  mfp_delete_data:
    RMDir /r "$APPDATA\media-fetch-pipeline"

  mfp_keep_data:
!macroend
