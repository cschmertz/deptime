# Sourced by the command hook. Leave stdin untouched for the Python script.
case ${OSTYPE-} in
    msys*|cygwin*)
        # Git Bash can see Microsoft Store aliases as executables. Search past them.
        shopt -s nocasematch
        set -f
        IFS='
'
        for interpreter in $(type -aP python.exe python3.exe 2>/dev/null); do
            case "$interpreter" in
                */WindowsApps/*) continue ;;
            esac
            exec "$interpreter" "$CLAUDE_PLUGIN_ROOT/scripts/suggest_deptime.py"
        done
        ;;
    *)
        if command -v python3 >/dev/null 2>&1; then
            exec python3 "$CLAUDE_PLUGIN_ROOT/scripts/suggest_deptime.py"
        fi
        ;;
esac
exit 0
