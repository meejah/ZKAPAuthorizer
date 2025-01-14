Contributing to ZKAPAuthorizer
==============================

Contributions are accepted in many forms.

Examples of contributions include:

* Bug reports and patch reviews
* Documentation improvements
* Code patches

File a ticket at:

https://github.com/PrivateStorageio/ZKAPAuthorizer/issues/new

ZKAPAuthorizer uses GitHub keep track of bugs, feature requests, and associated patches.

Contributions are managed using GitHub's Pull Requests.
For a PR to be accepted it needs to have:

* an associated issue
* all CI tests passing
* patch coverage of 100% as reported by codecov.io

Updating Dependencies
---------------------

We use `niv <https://github.com/nmattia/niv>`_ to manage several of our dependencies.

Python Dependencies
...................

We use `mach-nix <https://github.com/DavHau/mach-nix/>`_ to build python packages.
It uses a snapshot of PyPI to expose python dependencies to nix,
thus our python depedencies (on nix) are automatically pinned.
To update the PyPI snapshot (and thus our python dependencies), run

.. code:: shell

   nix-shell --run 'niv update pypi-deps-db'

tahoe-lafs
..........

ZKAPAuthorizer declares a dependency on Tahoe-LAFS with a narrow version range.
This means that Tahoe-LAFS will be installed when ZKAPAuthorizer is installed.
It also means that ZKAPAuthorizer exerts a great deal of control over the version of Tahoe-LAFS chosen.

When installing using native Python packaging mechanisms
(for example, pip)
the relevant Tahoe-LAFS dependency declaration is in ``setup.cfg``.
See the comments there about the narrow version constraint used.

When installing the Nix package the version of Tahoe-LAFS is determined by the "tahoe-lafs" entry in the niv-managed ``nix/sources.json``.
When feasible this is a released version of Tahoe-LAFS.
To update to a new release, run:

.. code:: shell

   nix-shell --run 'niv update --rev tahoe-lafs-A.B.C tahoe-lafs'

When it is not feasible to use a released version of Tahoe-LAFS,
niv's ``--branch`` or ``--rev`` features can be used to update this dependency.

It is also possible to pass a revision of ``pull/<pr-number>/head`` to test against a specific PR.

We test against a pinned commit of Tahoe-LAFS master.
To update to the current master@HEAD revision, run:

.. code:: shell

   nix-shell --run 'niv update tahoe-lafs-master --branch master'

We intend for these updates to be performed periodically.
At the moment, they must be performed manually.
It might be worthwhile to `automate this process <https://github.com/PrivateStorageio/ZKAPAuthorizer/issues/287>` in the future.

.. note::

   Since tahoe-lafs doesn't have correct version information when installed from a github archive,
   the packaging in ``default.nix`` includes a fake version number.
   This will need to be update manually at least when the minor version of tahoe-lafs changes.

If you want to test additional versions, you can add an additional source, pointing at other version.

.. code:: shell

   nix-shell --run 'niv add -n tahoe-lafs-next tahoe-lafs/tahoe-lafs --rev "<rev>"'
   nix-build tests.nix --argstr tahoe-lafs-source tahoe-lafs-next

``--argstr tahoe-lafs-source <...>`` can also be passed to ``nix-shell`` and ``nix-build default.nix``.

nixpkgs
.......

We pin to a nixos channel release, which isn't directly supported by niv (`issue <https://github.com/nmattia/niv/issues/225>`_).
Thus, the pin needs to be update manually.
To do this, copy the ``url`` and ``sha256`` values from PrivateStorageio's `nixpkgs-2105.json <https://whetstone.privatestorage.io/privatestorage/PrivateStorageio/-/blob/develop/nixpkgs-2105.json>`_ into the ``release2105`` entry in ``nix/sources.json``.
When this is deployed as part of Privatestorageio, we use the value pinned there, rather than the pin in this repository.
