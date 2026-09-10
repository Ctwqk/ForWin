#!/usr/bin/env bash
# Loaded from the frozen ForWin checkout by the 150 sync entry. No work on source.

forwin_build_image() {
  local image="$1" target="$2" revision="$3"
  [[ "$revision" =~ ^[0-9a-f]{40}$ ]] || { log "invalid ForWin source revision"; return 1; }
  [[ "$image" =~ ^[a-zA-Z0-9:._-]+$ ]] || return 1
  [[ "$target" =~ ^[a-zA-Z0-9_-]+$ ]] || return 1
  local command="docker build --build-arg FORWIN_SOURCE_REVISION=$revision --target $target -f Dockerfile -t $image ."
  if remote_sh 10.0.0.126 "cd /Users/magi1/ForWin-swarm && /opt/homebrew/bin/$command" >&2; then
    return 0
  fi
  log "ForWin build retry through Colima"
  remote_sh 10.0.0.126 "export PATH=/opt/homebrew/bin:/opt/homebrew/sbin:\$PATH; export LIMA_HOME=/Users/magi1/.colima/_lima; /opt/homebrew/bin/limactl shell colima-swarmbridged sh -c 'cd /Users/magi1/ForWin-swarm && $command'" >&2
}

forwin_deploy_project() {
  local project="$1" repo_name="$2" branch="$3" revision="$4"
  local app_image browser_image services images
  services="forwin-app-swarm forwin-mcp-swarm forwin-generation-worker-swarm forwin-publisher-worker-swarm forwin-outbox-worker-swarm forwin-publisher-browser-swarm"
  [[ "$revision" =~ ^[0-9a-f]{40}$ ]] || { log "invalid ForWin source revision"; return 1; }
  if [ "$MODE" = dry-run ]; then log "ForWin deployment skipped in dry-run"; return 0; fi
  if [ "$UPDATE_SERVICES" -eq 1 ] && [ "$HEALTH_CHECKS" -ne 1 ]; then
    log "ForWin refuses deployment with health checks disabled"; return 1
  fi
  app_image="$(image_tag forwin-forwin "$revision")"
  browser_image="$(image_tag forwin-publisher-browser "$revision")"
  images="$app_image $browser_image"
  if [ "$BUILD_IMAGES" -eq 1 ]; then
    forwin_build_image "$app_image" forwin-runtime "$revision" || return 1
    forwin_build_image "$browser_image" publisher-browser-runtime "$revision" || return 1
  elif [ "$UPDATE_SERVICES" -eq 0 ]; then
    record_state "$project" "$repo_name" "$branch" "$revision" "" "$services" synced
    return 0
  fi
  local arguments=(--revision "$revision" --app-image "$app_image" --browser-image "$browser_image" --state-dir "$STATE_DIR")
  if [ "$UPDATE_SERVICES" -eq 0 ]; then arguments+=(--verify-only); fi
  if ! python3 "$REPO_ROOT/ForWin/deploy/swarm/forwin_release.py" "${arguments[@]}"; then
    record_state "$project" "$repo_name" "$branch" "$revision" "$images" "$services" failed
    return 1
  fi
  record_state "$project" "$repo_name" "$branch" "$revision" "$images" "$services" "$(success_status)"
}

forwin_sync_and_deploy() {
  local project="$1" repo_name="$2" branch="$3" host="$4" target_dir="$5"
  shift 5
  local revision rc
  revision="$(repo_commit "$repo_name")" || return $?
  # Explicit error propagation also holds when the parent runs this in an if.
  if sync_stage_to_target "$project" "$repo_name" "$branch" "$host" "$target_dir" "$@"; then
    forwin_deploy_project "$project" "$repo_name" "$branch" "$revision" || return $?
    write_target_marker "$project" "$repo_name" "$branch" "$host" "$target_dir" "$revision" || return $?
    return 0
  else
    rc=$?
    [ "$rc" -eq 10 ] && return 0
    return "$rc"
  fi
}
