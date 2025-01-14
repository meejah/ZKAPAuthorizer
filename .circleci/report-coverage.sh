#! /usr/bin/env nix-shell
#! nix-shell -i bash -p "curl" -p "python.withPackages (ps: [ ps.coverage ])"
set -x
find ./result*/
cp ./result*/coverage/.coverage ./.coverage
python -m coverage xml

# Unfortunately, this is the recommended uploader.
# https://docs.codecov.io/docs/about-the-codecov-bash-uploader
bash <(curl -s https://codecov.io/bash)
