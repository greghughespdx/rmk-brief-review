# shellcheck shell=bash
# Source this from every workflow script:  . "$DIR/rmw-env.sh"
#
# Loads the config file (see rmw_config.py) into RMW_* environment variables.
# A variable that is already set wins, so a test or a one-off run can override
# any single value. Fails loudly when the config file cannot be read.
_rmw_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_rmw_py="${RMW_RENDER_PYTHON:-${RMW_CONFIG_PYTHON:-python3}}"
if ! _rmw_lines="$("$_rmw_py" "$_rmw_dir/rmw_config.py" shell)"; then
  echo "rmw-env.sh: could not load the remarkable-workflow config" >&2
  return 2 2>/dev/null || exit 2
fi
eval "$_rmw_lines"
unset _rmw_lines _rmw_py _rmw_dir
