# -*- coding: utf-8 -*-

import os.path
import io
import pkg_resources
import sys
from packaging.version import parse as parse_version

import pytest

__version__ = "1.0.3"

DEFAULT_PATH = "test-output.xml"
DEFAULT_COVERAGE_PATH = "coverage/coverage.xml"
DEFAULT_HTML_COVERAGE_PATH= "coverage/htmlcov"

def pytest_addoption(parser):
    group = parser.getgroup("pytest_azurepipelines")
    group.addoption(
        "--test-run-title",
        action="store",
        dest="azure_run_title",
        default="Pytest results",
        help="Set the Azure test run title.",
    )
    group.addoption(
        "--napoleon-docstrings",
        action="store_true",
        dest="napoleon",
        default=False,
        help="If using Google, NumPy, or PEP 257 multi-line docstrings.",
    )
    group.addoption(
        "--no-coverage-upload",
        action="store_true",
        dest="no_coverage_upload",
        default=False,
        help="Skip uploading coverage results to Azure Pipelines.",
    )
    group.addoption(
        "--no-docker-discovery",
        action="store_true",
        dest="no_docker_discovery",
        default=False,
        help="Skip detecting running inside a Docker container.",
    )

@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    xmlpath = config.getoption("--nunitxml")
    if not xmlpath:
        config.option.nunit_xmlpath = DEFAULT_PATH

    # ensure coverage creates xml format
    if config.pluginmanager.has_plugin("pytest_cov"):
        config.option.cov_report["xml"] = os.path.normpath(
            os.path.abspath(os.path.expanduser(os.path.expandvars(DEFAULT_COVERAGE_PATH)))
        )
        if "html" not in config.option.cov_report:
            config.option.cov_report["html"] = None


def get_resource_folder_path():
    resources_folder_name = "resources"
    ancestor = pkg_resources.resource_filename(__name__, "")

    # traverse to parent folder until a child folder with name "resources"
    # is found, or the root is reached
    while not os.path.exists(os.path.join(ancestor, resources_folder_name)):
        ancestor = os.path.dirname(ancestor)

        if not ancestor or ancestor == "/":
            if os.path.exists(resources_folder_name):
                break
            raise RuntimeError("Could not find the path to resources folder.")

    return os.path.join(ancestor, resources_folder_name)


def get_resource_file_content(file_name):
    with open(os.path.join(get_resource_folder_path(), file_name), mode='rt') as source:
        return source.read()


def inline_css_into_each_html_report_file(reportdir):
    """
    Since <link> does not work inside the iframe used by Azure DevOps,
    inline the CSS styles into each HTML file generated by pytest report.
    This enables a good UX when reading reports in the portal.
    """
    style_fragment = "\n<style>\n" + get_resource_file_content("style.css") + "\n</style>\n"

    # since pytest-cov generates a flat folder, we don't need recursion here
    for file in os.listdir(reportdir):
        if file.endswith(".html"):
            full_path = os.path.join(reportdir, file)

            with open(full_path, mode="rt", encoding="utf8") as f:
                new_text = f.read().replace("</head>", style_fragment + "</head>")

            with open(full_path, mode="wt", encoding="utf8") as f:
                f.write(new_text)


def try_to_inline_css_into_each_html_report_file(reportdir):
    try:
        inline_css_into_each_html_report_file(reportdir)
    except Exception as ex:
        print(
            "##vso[task.logissue type=warning;]{0}{1}".format(
                "Failed to inline CSS styles in coverage reports. Error: ",
                str(ex)
            )
        )


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    xmlpath = session.config.option.nunit_xmlpath
    mode = "NUnit"

    # This mirrors https://github.com/pytest-dev/pytest/blob/38adb23bd245329d26b36fd85a43aa9b3dd0406c/src/_pytest/junitxml.py#L368-L369
    xmlabspath = os.path.normpath(
        os.path.abspath(os.path.expanduser(os.path.expandvars(xmlpath)))
    )
    mountinfo = None
    if not session.config.getoption("no_docker_discovery") and os.path.isfile('/.dockerenv'):
        with io.open(
                    '/proc/1/mountinfo', 'rb',
                ) as fobj:
            mountinfo = fobj.read()
        mountinfo = mountinfo.decode(sys.getfilesystemencoding())
    if mountinfo:
        xmlabspath = apply_docker_mappings(mountinfo, xmlabspath)

    # Set the run title in the UI to a configurable setting
    description = session.config.option.azure_run_title.replace("'", "")

    if not session.config.getoption("no_docker_discovery"):
        print(
            "##vso[results.publish type={2};runTitle='{1}';publishRunAttachments=true;]{0}".format(
                xmlabspath, description, mode
            )
        )
    else:
        print("Skipping uploading of test results because --no-docker-discovery set.")

    if exitstatus != 0 and session.testsfailed > 0 and not session.shouldfail:
        print(
            "##vso[task.logissue type=error;]{0} test(s) failed, {1} test(s) collected.".format(
                session.testsfailed, session.testscollected
            )
        )

    if not session.config.getoption("no_coverage_upload") and not session.config.getoption("no_docker_discovery") and session.config.pluginmanager.has_plugin("pytest_cov"):
        covpath = os.path.normpath(
            os.path.abspath(os.path.expanduser(os.path.expandvars(DEFAULT_COVERAGE_PATH)))
        )
        reportdir = os.path.normpath(os.path.abspath(DEFAULT_HTML_COVERAGE_PATH))
        if os.path.exists(covpath):
            if mountinfo:
                covpath = apply_docker_mappings(mountinfo, covpath)
                reportdir = apply_docker_mappings(mountinfo, reportdir)

            try_to_inline_css_into_each_html_report_file(reportdir)
            print(
                "##vso[codecoverage.publish codecoveragetool=Cobertura;summaryfile={0};reportdirectory={1};]".format(
                    covpath, reportdir
                )
            )
        else:
            print(
                "##vso[task.logissue type=warning;]{0}".format(
                    "Coverage XML was not created, skipping upload."
                )
            )
    else:
        print("Skipping uploading of coverage data.")


def apply_docker_mappings(mountinfo, dockerpath):
    """
    Parse the /proc/1/mountinfo file and apply the mappings so that docker
    paths are transformed into the host path equivalent so the Azure Pipelines
    finds the file assuming the path has been bind mounted from the host.
    """
    for line in mountinfo.splitlines():
        words = line.split(' ')
        if len(words) < 5:
            continue
        docker_mnt_path = words[4]
        host_mnt_path = words[3]
        if dockerpath.startswith(docker_mnt_path):
            dockerpath = ''.join([
                host_mnt_path,
                dockerpath[len(docker_mnt_path):],
            ])
    return dockerpath

if parse_version(pytest.__version__) >= parse_version("7.0.0"):
    def pytest_warning_recorded(warning_message, *args, **kwargs):
        print("##vso[task.logissue type=warning;]{0}".format(str(warning_message.message)))
else:
    def pytest_warning_captured(warning_message, *args, **kwargs):
        print("##vso[task.logissue type=warning;]{0}".format(str(warning_message.message)))


@pytest.fixture
def record_pipelines_property(record_nunit_property):
    # Proxy for Nunit fixture, just in case we later change the API
    return record_nunit_property


@pytest.fixture
def add_pipelines_attachment(add_nunit_attachment):
    # Proxy for Nunit fixture, just in case we later change the API
    return add_nunit_attachment
