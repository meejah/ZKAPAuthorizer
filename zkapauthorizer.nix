{ buildPythonPackage, sphinx, circleci-cli
, attrs, zope_interface, twisted, tahoe-lafs, privacypass
, fixtures, testtools, hypothesis, pyflakes, treq, coverage
, hypothesisProfile ? null
, collectCoverage ? false
, testSuite ? null
, trialArgs ? []
}:
let
  hypothesisProfile' = if hypothesisProfile == null then "default" else hypothesisProfile;
  testSuite' = if testSuite == null then "_zkapauthorizer" else testSuite;
  extraTrialArgs = builtins.concatStringsSep " " trialArgs;
in
buildPythonPackage rec {
  version = "0.0";
  pname = "zero-knowledge-access-pass-authorizer";
  name = "${pname}-${version}";
  src = ./.;

  outputs = [ "out" ] ++ (if collectCoverage then [ "doc" ] else [ ]);

  depsBuildBuild = [
    sphinx
    circleci-cli
  ];

  propagatedBuildInputs = [
    attrs
    zope_interface
    twisted
    tahoe-lafs
    privacypass
  ];

  checkInputs = [
    coverage
    fixtures
    testtools
    hypothesis
    twisted
    treq
  ];

  checkPhase = ''
    runHook preCheck
    set -x
    "${pyflakes}/bin/pyflakes" src/_zkapauthorizer
    ZKAPAUTHORIZER_HYPOTHESIS_PROFILE=${hypothesisProfile'} python -m ${if collectCoverage
      then "coverage run --branch --source _zkapauthorizer,twisted.plugins.zkapauthorizer --module"
      else ""
    } twisted.trial ${extraTrialArgs} ${testSuite'}
    runHook postCheck
  '';

  postCheck = if collectCoverage
    then ''
    python -m coverage html
    mkdir -p "$doc/share/doc/${name}"
    cp -vr .coverage htmlcov "$doc/share/doc/${name}"
    python -m coverage report
    ''
    else "";
}