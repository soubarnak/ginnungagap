# Spaces shell integration for fish. OPT-IN: copy or link this file to
# ~/.config/fish/conf.d/spaces.fish (see spaces.sh for what it does).
if command -q spaces
    function arch; test "$argv[1]" = --; and set -e argv[1]; spaces enter arch -- $argv; end
    function ubuntu; test "$argv[1]" = --; and set -e argv[1]; spaces enter ubuntu -- $argv; end
    function fedora; test "$argv[1]" = --; and set -e argv[1]; spaces enter fedora -- $argv; end
    function kali; test "$argv[1]" = --; and set -e argv[1]; spaces enter kali -- $argv; end

    if test -z "$SPACES_NO_HINTS"
        function __spaces_hint
            printf '%s: not installed on this Void host. Run it in the %s space instead: `%s %s ...`\n' $argv[1] $argv[2] $argv[2] $argv[1] >&2
            return 127
        end
        command -q apt; or function apt; __spaces_hint apt ubuntu; end
        command -q apt-get; or function apt-get; __spaces_hint apt-get ubuntu; end
        command -q dnf; or function dnf; __spaces_hint dnf fedora; end
    end
end
