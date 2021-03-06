#!/usr/bin/env python

from ocp_cd_tools import Runtime, Dir
from ocp_cd_tools.image import pull_image, create_image_verify_repo_file, Image
from ocp_cd_tools.model import Missing
from ocp_cd_tools.brew import get_watch_task_info_copy
from ocp_cd_tools import constants
from ocp_cd_tools import metadata
from ocp_cd_tools.config import MetaDataConfig as mdc
from ocp_cd_tools.config import valid_updates
import datetime
import click
import os
import shutil
import yaml
import sys
import subprocess
import urllib
import traceback
import koji
from numbers import Number
from multiprocessing.dummy import Pool as ThreadPool
from multiprocessing import cpu_count
from dockerfile_parse import DockerfileParser

pass_runtime = click.make_pass_decorator(Runtime)
context_settings = dict(help_option_names=['-h', '--help'])


# ============================================================================
# GLOBAL OPTIONS: parameters for all commands
# ============================================================================
@click.group(context_settings=context_settings)
@click.option("--metadata-dir", metavar='PATH', default=None,
              help="DEPRECATED. For development use only!. Git repo or directory containing groups metadata directory if not current.")
@click.option("--working-dir", metavar='PATH', envvar="OIT_WORKING_DIR",
              default=None,
              help="Existing directory in which file operations should be performed.\n Env var: OIT_WORKING_DIR")
@click.option("--user", metavar='USERNAME', envvar="OIT_USER",
              default=None,
              help="Username for rhpkg. Env var: OIT_USER")
@click.option("-g", "--group", default=None, metavar='NAME',
              help="The group of images on which to operate.")
@click.option("--branch", default=None, metavar='BRANCH',
              help="Branch to override any default in group.yml.")
@click.option('--stage', default=False, is_flag=True, help='Force checkout stage branch for sources in group.yml.')
@click.option("-i", "--images", default=[], metavar='NAME', multiple=True,
              help="Name of group image member to include in operation (all by default). Can be comma delimited list.")
@click.option("-r", "--rpms", default=[], metavar='NAME', multiple=True,
              help="Name of group rpm member to include in operation (all by default). Can be comma delimited list.")
@click.option('--wip', default=False, is_flag=True, help='Load WIP RPMs/Images in addition to those specified, if any')
@click.option("-x", "--exclude", default=[], metavar='NAME', multiple=True,
              help="Name of group image or rpm member to exclude in operation (none by default). Can be comma delimited list.")
@click.option('--ignore-missing-base', default=False, is_flag=True,
              help='If a base image is not included, proceed and do not update FROM.')
@click.option('--latest-parent-version', default=False, is_flag=True,
              help='If a base image is not included, lookup latest FROM tag for parent. Implies --ignore-missing-base')
@click.option("--quiet", "-q", default=False, is_flag=True, help="Suppress non-critical output")
@click.option('--debug', default=False, is_flag=True, help='Show debug output on console.')
@click.option('--no_oit_comment', default=False, is_flag=True,
              help='Do not place OIT comment in Dockerfile. Can also be set in each config yaml')
@click.option("--source", metavar="ALIAS PATH", nargs=2, multiple=True,
              help="Associate a path with a given source alias.  [multiple]")
@click.option("--sources", metavar="YAML_PATH",
              help="YAML dict associating sources with their alias. Same as using --source multiple times.")
@click.option('--odcs-mode', default=False, is_flag=True,
              help='Process Dockerfiles in ODCS mode. HACK for the time being.')
@click.option('--disabled', default=False, is_flag=True,
              help='Treat disabled images/rpms as if they were enabled')
@click.pass_context
def cli(ctx, **kwargs):
    # @pass_runtime
    ctx.obj = Runtime(**kwargs)


option_commit_message = click.option("--message", "-m", metavar='MSG', help="Commit message for dist-git.",
                                     required=True)
option_push = click.option('--push/--no-push', default=False, is_flag=True,
                           help='Pushes to distgit after local changes (--no-push by default).')

# =============================================================================
#
# CLI Commands
#
# =============================================================================

@cli.command("images:clone", help="Clone a group's image distgit repos locally.")
@pass_runtime
def images_clone(runtime):
    runtime.initialize(clone_distgits=True)
    # Never delete after clone; defeats the purpose of cloning
    runtime.remove_tmp_working_dir = False


@cli.command("rpms:clone", help="Clone a group's rpm distgit repos locally.")
@pass_runtime
def rpms_clone(runtime):
    runtime.initialize(mode='rpms', clone_distgits=True)
    # Never delete after clone; defeats the purpose of cloning
    runtime.remove_tmp_working_dir = False


@cli.command("rpms:clone-sources", help="Clone a group's rpm source repos locally and add to sources yaml.")
@click.option("--output-yml", metavar="YAML_PATH",
              help="Output yml file to write sources dict to. Can be same as --sources option but must be explicitly specified.")
@pass_runtime
def rpms_clone_sources(runtime, output_yml):
    runtime.initialize(mode='rpms')
    # Never delete after clone; defeats the purpose of cloning
    runtime.remove_tmp_working_dir = False
    [r for r in runtime.rpm_metas()]
    if output_yml:
        runtime.export_sources(output_yml)


@cli.command("rpms:build", help="Build rpms in the group or given by --rpms.")
@click.option("--version", metavar='VERSION', default=None,
              help="Version string to populate in specfile.", required=True)
@click.option("--release", metavar='RELEASE', default=None,
              help="Release label to populate in specfile.", required=True)
@click.option('--scratch', default=False, is_flag=True, help='Perform a scratch build.')
@pass_runtime
def rpms_build(runtime, version, release, scratch):
    """
    Attempts to build rpms for all of the defined rpms
    in a group. If an rpm has already been built, it will be treated as
    a successful operation.
    """

    if version.startswith('v'):
        version = version[1:]

    runtime.initialize(mode='rpms', clone_distgits=False)

    items = runtime.rpm_metas()
    if not items:
        runtime.logger.info("No RPMs found. Check the arguments.")
        exit(0)

    results = runtime.parallel_exec(
        lambda (rpm, terminate_event): rpm.build_rpm(
            version, release, terminate_event, scratch),
        items)
    results = results.get()
    failed = [m.distgit_key for m, r in zip(runtime.rpm_metas(), results) if not r]
    if failed:
        runtime.logger.error("\n".join(["Build/push failures:"] + sorted(failed)))
        exit(1)


@cli.command("images:list", help="List of distgits being selected.")
@pass_runtime
def images_list(runtime):
    runtime.initialize(clone_distgits=False)

    click.echo("------------------------------------------")
    for image in runtime.image_metas():
        click.echo(image.qualified_name)
    click.echo("------------------------------------------")
    click.echo("%s images" % len(runtime.image_metas()))


@cli.command("images:push-distgit", short_help="Push all distgist repos in working-dir.")
@pass_runtime
def images_push_distgit(runtime):
    """
    Run to execute an rhpkg push on all locally cloned distgit
    repositories. This is useful following a series of modifications
    in the local clones.
    """
    runtime.initialize(clone_distgits=True)
    runtime.push_distgits()


@cli.command("images:update-dockerfile", short_help="Update a group's distgit Dockerfile from metadata.")
@click.option("--stream", metavar="ALIAS REPO/NAME:TAG", nargs=2, multiple=True,
              help="Associate an image name with a given stream alias.  [multiple]")
@click.option("--version", metavar='VERSION', default=None,
              help="Version string to populate in Dockerfiles. \"auto\" gets version from atomic-openshift RPM")
@click.option("--release", metavar='RELEASE', default=None,
              help="Release label to populate in Dockerfiles (or + to bump).")
@click.option("--repo-type", metavar="REPO_TYPE", envvar="OIT_IMAGES_REPO_TYPE",
              default="unsigned",
              help="Repo group type to use for version autodetection scan (e.g. signed, unsigned).")
@option_commit_message
@option_push
@pass_runtime
def images_update_dockerfile(runtime, stream, version, release, repo_type, message, push):
    """
    Updates the Dockerfile in each distgit repository with the latest metadata and
    the version/release information specified. This does not update the Dockerfile
    from any external source. For that, use images:rebase.

    Version:
    - If not specified, the current version is preserved.

    Release:
    - If not specified, the release label is removed.
    - If '+', the current release will be bumped.
    - Else, the literal value will be set in the Dockerfile.
    """
    runtime.initialize(validate_content_sets=True)

    # If not pushing, do not clean up our work
    runtime.remove_tmp_working_dir = push

    # For each "--stream alias image" on the command line, register its existence with
    # the runtime.
    for s in stream:
        runtime.register_stream_alias(s[0], s[1])

    # Get the version from the atomic-openshift package in the RPM repo
    if version == "auto":
        version = runtime.auto_version(repo_type)

    if version and not runtime.valid_version(version):
        raise ValueError(
            "invalid version string: {}, expecting like v3.4 or v1.2.3".format(version)
        )

    runtime.clone_distgits()
    for image in runtime.image_metas():
        dgr = image.distgit_repo()
        (real_version, real_release) = dgr.update_distgit_dir(version, release)
        dgr.commit(message)
        dgr.tag(real_version, real_release)

    if push:
        runtime.push_distgits()


@cli.command("images:verify", short_help="Run some smoke tests to verify produced images")
@click.option("--image", "-i", metavar="REPO/NAME:TAG", multiple=True,
              help="Run a manual scan of one or more specific images [multiple]")
@click.option("--no-pull", is_flag=True,
              help="Assume image has already been pulled (Just-in-time pulling)")
@click.option("--repo-type", metavar="REPO_TYPE", envvar="OIT_IMAGE_REPO_TYPE",
              default='unsigned',
              help="Repo type (e.g. signed, unsigned). Use 'unsigned' for newer releases like 3.9 until they're GA")
@click.option('--check-orphans', default=None, is_flag=True,
              help="Verify no packages are orphaned (installed without a source repo) [SLOW]")
@click.option('--check-sigs', default=None, is_flag=True,
              help="Verify that all installed packages are signed with a valid key")
@click.option('--check-versions', default=None, is_flag=True,
              help="Verify that installed package versions match the target release")
@pass_runtime
def images_verify(runtime, image, no_pull, repo_type, **kwargs):
    """Catches mistakes in images (see the --check-FOO options) before we
    ship them on to QE for further verification. This command roughly
    approximates the image check job ran on the QE Jenkins.

    Ensure you provide a `--working-dir` if you want to retain the
    test reports. Some useful artifacts are produced in the
    `working-dir`: debug.log: The full verbose log of all
    operations. verify_fail_log.yml: Failed images check
    details. verify_full_log.yml: Full image check details.

    EXAMPLES

    * Run all checks on all the lastest openshift-3.9 group images:

      $ oit.py --group openshift-3.9 images:verify

    * ONLY the package signature check:

      $ oit.py --group openshift-3.9 images:verify --check-sigs

    * All tests on a SPECIFIC image:

      \b
      $ oit.py --group openshift-3.4 images:verify --image reg.rh.com:8888/openshift3/ose:v3.4.1.44.38-12

    Exit code is the number of images that failed the image check

    """
    # * Job: https://openshift-qe-jenkins.rhev-ci-vms.eng.rdu2.redhat.com/job/v3-errata-image-test/
    # * Source: git://git.app.eng.bos.redhat.com/openshift-misc.git/openshift-misc/v3-errata-image-test/v3-errata-image-test.sh
    runtime.initialize(clone_distgits=False)
    # Parse those check options. All None indicates we run everything,
    # True then we only run the True checks.
    if not any(kwargs.values()):
        enabled_checks = kwargs.keys()
    else:
        enabled_checks = list(k for k, v in kwargs.items() if v)

    repo_file = create_image_verify_repo_file(runtime, repo_type=repo_type)

    # Doing this manually, or automatic on a group?
    if len(image) == 0:
        images = [Image(runtime, x.pull_url(), repo_file, enabled_checks, distgit=x.name) for x in runtime.image_metas()]
    else:
        images = [Image(runtime, img, repo_file, enabled_checks) for img in image]

    # Don't pre-pull images, useful during development iteration. If
    # --no-pull isn't given, then we will pre-pull all images.
    if not no_pull:
        for image in images:
            pull_image(image.pull_url)

    count_images = len(images)
    runtime.logger.info("[Verify] Running verification checks on {count} images: {chks}".format(count=count_images, chks=", ".join(enabled_checks)))

    pool = ThreadPool(cpu_count())
    results = pool.map(
        lambda img: img.verify_image(),
        images)
    # Wait for results
    pool.close()
    pool.join()

    ######################################################################
    # Done! Let's begin accounting and recording
    failed_images = [img for img in results if img['status'] == 'failed']
    failed_images_count = len(failed_images)

    fail_log = os.path.join(runtime.working_dir, 'verify_fail_log.yml')

    runtime.logger.info("[Verify] Checks finished. {fail_count} failed".format(fail_count=failed_images_count))

    if failed_images_count > 0:
        runtime.remove_tmp_working_dir = False
        fail_log_data = {
            'test_date': str(datetime.datetime.now()),
            'data': failed_images
        }

        with open(fail_log, 'w') as fp:
            verbose_fail = yaml.safe_dump(fail_log_data, indent=4, default_flow_style=False)
            fp.write(verbose_fail)
            runtime.logger.info("[Verify] Failed images check details: {fail_log}".format(fail_log=fail_log))
            runtime.logger.info("[Verify] Verbose failure: {vfail}".format(vfail=verbose_fail))

    exit(failed_images_count)


@cli.command("images:rebase", short_help="Refresh a group's distgit content from source content.")
@click.option("--stream", metavar="ALIAS REPO/NAME:TAG", nargs=2, multiple=True,
              help="Associate an image name with a given stream alias.  [multiple]")
@click.option("--version", metavar='VERSION', default=None,
              help="Version string to populate in Dockerfiles. \"auto\" gets version from atomic-openshift RPM")
@click.option("--release", metavar='RELEASE', default=None, help="Release string to populate in Dockerfiles.")
@click.option("--repo-type", metavar="REPO_TYPE", envvar="OIT_IMAGES_REPO_TYPE",
              default="unsigned",
              help="Repo group type to use for version autodetection scan (e.g. signed, unsigned).")
@option_commit_message
@option_push
@pass_runtime
def images_rebase(runtime, stream, version, release, repo_type, message, push):
    """
    Many of the Dockerfiles stored in distgit are based off of content managed in GitHub.
    For example, openshift-enterprise-node should always closely reflect the changes
    being made upstream in github.com/openshift/ose/images/node. This operation
    goes out and pulls the current source Dockerfile (and potentially other supporting
    files) into distgit and applies any transformations defined in the config yaml associated
    with the distgit repo.

    This operation will also set the version and release in the file according to the
    command line arguments provided.

    If a distgit repo does not have associated source (i.e. it is managed directly in
    distgit), the Dockerfile in distgit will not be rebased, but other aspects of the
    metadata may be applied (base image, tags, etc) along with the version and release.
    """
    runtime.initialize(validate_content_sets=True)

    # If not pushing, do not clean up our work
    runtime.remove_tmp_working_dir = push

    # For each "--stream alias image" on the command line, register its existence with
    # the runtime.
    for s in stream:
        runtime.register_stream_alias(s[0], s[1])

    # Get the version from the atomic-openshift package in the RPM repo
    if version == "auto":
        version = runtime.auto_version(repo_type)

    if not runtime.valid_version(version):
        raise ValueError(
            "invalid version string: {}, expecting like v3.4 or v1.2.3".format(version)
        )

    runtime.clone_distgits()
    for image in runtime.image_metas():
        dgr = image.distgit_repo()
        (real_version, real_release) = dgr.rebase_dir(version, release)
        sha = dgr.commit(message, log_diff=True)
        dgr.tag(real_version, real_release)
        runtime.add_record(
            "distgit_commit",
            distgit=dgr.metadata.qualified_name,
            image=dgr.config.name,
            sha=sha)

    if push:
        runtime.push_distgits()


@cli.command("images:foreach", short_help="Run a command relative to each distgit dir.")
@click.argument("cmd", nargs=-1)
@click.option("--message", "-m", metavar='MSG', help="Commit message for dist-git.", required=False)
@option_push
@pass_runtime
def images_foreach(runtime, cmd, message, push):
    """
    Clones all distgit repos found in the specified group and runs an arbitrary
    command once for each local distgit directory. If the command runs without
    error for all directories, a commit will be made. If not a dry_run,
    the repo will be pushed.

    \b
    The following environment variables will be available in each invocation:
    oit_repo_name : The name of the distgit repository
    oit_repo_namespace : The distgit repository namespaces (e.g. containers, rpms))
    oit_config_filename : The config yaml (basename, no path) associated with an image
    oit_distgit_key : The name of the distgit_key used with -i, -x for this image
    oit_image_name : The name of the image from Dockerfile
    oit_image_version : The current version found in the Dockerfile
    oit_group: The group for this invocation
    oit_metadata_dir: The directory containing the oit metadata
    oit_working_dir: The current working directory
    """
    runtime.initialize(clone_distgits=True)

    # If not pushing, do not clean up our work
    runtime.remove_tmp_working_dir = push

    cmd_str = " ".join(cmd)

    for image in runtime.image_metas():
        dgr = image.distgit_repo()
        with Dir(dgr.distgit_dir):
            runtime.logger.info("Executing in %s: [%s]" % (dgr.distgit_dir, cmd_str))

            dfp = DockerfileParser()
            dfp.content = image.fetch_cgit_file("Dockerfile")

            if subprocess.call(cmd_str,
                               shell=True,
                               env={"oit_repo_name": image.name,
                                    "oit_repo_namespace": image.namespace,
                                    "oit_image_name": dfp.labels["name"],
                                    "oit_image_version": dfp.labels["version"],
                                    "oit_group": runtime.group,
                                    "oit_metadata_dir": runtime.metadata_dir,
                                    "oit_working_dir": runtime.working_dir,
                                    "oit_config_filename": image.config_filename,
                                    "oit_distgit_key": image.distgit_key,
                                    }) != 0:
                raise IOError("Command return non-zero status")
            runtime.logger.info("\n")

        if message is not None:
            dgr.commit(message)

    if push:
        runtime.push_distgits()


@cli.command("images:revert", help="Revert a fixed number of commits in each distgit.")
@click.argument("count", nargs=1)
@click.option("--message", "-m", metavar='MSG', help="Commit message for dist-git.", default=None, required=False)
@option_push
@pass_runtime
def images_revert(runtime, count, message, push):
    """
    Revert a particular number of commits in each distgit repository. If
    a message is specified, a new commit will be made.
    """
    runtime.initialize()

    # If not pushing, do not clean up our work
    runtime.remove_tmp_working_dir = push

    count = int(count) - 1
    if count < 0:
        runtime.logger.info("Revert count must be >= 1")

    if count == 0:
        commit_range = "HEAD"
    else:
        commit_range = "HEAD~%s..HEAD" % count

    cmd = ["git", "revert", "--no-commit", commit_range]

    cmd_str = " ".join(cmd)
    runtime.clone_distgits()
    dgrs = [image.distgit_repo() for image in runtime.image_metas()]
    for dgr in dgrs:
        with Dir(dgr.distgit_dir):
            runtime.logger.info("Running revert in %s: [%s]" % (dgr.distgit_dir, cmd_str))
            if subprocess.call(cmd_str, shell=True) != 0:
                raise IOError("Command return non-zero status")
            runtime.logger.info("\n")

        if message is not None:
            dgr.commit(message)

    if push:
        runtime.push_distgits()


@cli.command("images:merge-branch", help="Copy content of source branch to target.")
@click.option("--target", metavar="TARGET_BRANCH", help="Branch to populate from source branch.")
@click.option('--allow-overwrite', default=False, is_flag=True,
              help='Merge in source branch even if Dockerfile already exists in distgit')
@option_push
@pass_runtime
def images_merge(runtime, target, push, allow_overwrite):
    """
    For each distgit repo, copies the content of the group's branch to a new
    branch.
    """
    runtime.initialize()

    # If not pushing, do not clean up our work
    runtime.remove_tmp_working_dir = push

    runtime.clone_distgits()
    dgrs = [image.distgit_repo() for image in runtime.image_metas()]
    for dgr in dgrs:
        with Dir(dgr.distgit_dir):
            dgr.logger.info("Merging from branch {} to {}".format(dgr.branch, target))
            dgr.merge_branch(target, allow_overwrite)
            runtime.logger.info("\n")

    if push:
        runtime.push_distgits()


def _taskinfo_has_timestamp(task_info, key_name):
    """
    Tests to see if a named timestamp exists in a koji taskinfo
    dict.
    :param task_info: The taskinfo dict to check
    :param key_name: The name of the timestamp key
    :return: Returns True if the timestamp is found and is a Number
    """
    return isinstance(task_info.get(key_name, None), Number)


def print_build_metrics(runtime):
    watch_task_info = get_watch_task_info_copy()
    runtime.logger.info("\n\n\nImage build metrics:")
    runtime.logger.info("Number of brew tasks attempted: {}".format(len(watch_task_info)))

    # Make sure all the tasks have the expected timestamps:
    # https://github.com/openshift/enterprise-images/pull/178#discussion_r173812940
    for task_id in watch_task_info.keys():
        info = watch_task_info[task_id]
        runtime.logger.debug("Watch task info:\n {}\n\n".format(info))
        # Error unless all true
        if not ('id' in info and
                koji.TASK_STATES[info['state']] is 'CLOSED' and
                _taskinfo_has_timestamp(info, 'create_ts') and
                _taskinfo_has_timestamp(info, 'start_ts') and
                _taskinfo_has_timestamp(info, 'completion_ts')
                ):
            runtime.logger.error(
                "Discarding incomplete/error task info: {}".format(info))
            del watch_task_info[task_id]

    runtime.logger.info("Number of brew tasks successful: {}".format(len(watch_task_info)))

    # An estimate of how long the build time was extended due to FREE state (i.e. waiting for capacity)
    elapsed_wait_minutes = 0

    # If two builds each take one minute of actual active CPU time to complete, this value will be 2.
    aggregate_build_secs = 0

    # If two jobs wait 1m for in FREE state, this value will be '2' even if
    # the respective wait periods overlap. This is different from elapsed_wait_minutes
    # which is harder to calculate.
    aggregate_wait_secs = 0

    # Will be populated with earliest creation timestamp found in all the koji tasks; initialize with
    # infinity to support min() logic.
    min_create_ts = float('inf')

    # Will be populated with the latest completion timestamp found in all the koji tasks
    max_completion_ts = 0

    # Loop through all koji task infos and calculate min
    for task_id, info in watch_task_info.iteritems():
        create_ts = info['create_ts']
        completion_ts = info['completion_ts']
        start_ts = info['start_ts']
        min_create_ts = min(create_ts, min_create_ts)
        max_completion_ts = max(completion_ts, max_completion_ts)
        build_secs = completion_ts - start_ts
        aggregate_build_secs += build_secs
        wait_secs = start_ts - create_ts
        aggregate_wait_secs += wait_secs

        runtime.logger.info('Task {} took {:.1f}m of active build and was waiting to start for {:.1f}m'.format(
            task_id,
            build_secs / 60.0,
            wait_secs / 60.0))
    runtime.logger.info('Aggregate time all builds spent building {:.1f}m'.format(aggregate_build_secs / 60.0))
    runtime.logger.info('Aggregate time all builds spent waiting {:.1f}m'.format(aggregate_wait_secs / 60.0))

    # If we successfully found timestamps in completed builds
    if watch_task_info:

        # For each minute which elapsed between the first build created (min_create_ts) to the
        # last build to complete (max_completion_ts), check whether there was any build that
        # was created but still waiting to start (i.e. in FREE state). If there is a build
        # waiting, include that minute in the elapsed wait time.

        for ts in xrange(int(min_create_ts), int(max_completion_ts), 60):
            # See if any of the tasks were created but not started during this minute
            for info in watch_task_info.itervalues():
                create_ts = int(info['create_ts'])
                start_ts = int(info['start_ts'])
                # Was the build waiting to start during this minute?
                if create_ts <= ts <= start_ts:
                    # Increment and exit; we don't want to count overlapping wait periods
                    # since it would not accurately reflect the overall time savings we could
                    # expect with more capacity.
                    elapsed_wait_minutes += 1
                    break

        runtime.logger.info("Approximate elapsed time (wasted) waiting: {}m".format(elapsed_wait_minutes))
        elapsed_total_minutes = (max_completion_ts - min_create_ts) / 60.0
        runtime.logger.info("Elapsed time (from first submit to last completion) for all builds: {:.1f}m".format(elapsed_total_minutes))

        runtime.add_record("image_build_metrics", elapsed_wait_minutes=int(elapsed_wait_minutes),
                           elapsed_total_minutes=int(elapsed_total_minutes), task_count=len(watch_task_info))
    else:
        runtime.logger.info('Unable to determine timestamps from collected info: {}'.format(watch_task_info))


@cli.command("images:build", short_help="Build images for the group.")
@click.option("--odcs", default=None, metavar="ODCS",
              help="ODCS signing intent (e.g. signed, unsigned).")
@click.option("--repo-type", metavar="REPO_TYPE", envvar="OIT_IMAGES_REPO_TYPE",
              default=None,
              help="Repo type (e.g. signed, unsigned).")
@click.option("--repo", default=[], metavar="REPO_URL",
              multiple=True, help="Custom repo URL to supply to brew build.")
@click.option('--push-to-defaults', default=False, is_flag=True,
              help='Push to default registries when build completes.')
@click.option("--push-to", default=[], metavar="REGISTRY", multiple=True,
              help="Specific registries to push to when image build completes.  [multiple]")
@click.option('--scratch', default=False, is_flag=True, help='Perform a scratch build.')
@pass_runtime
def images_build_image(runtime, odcs, repo_type, repo, push_to_defaults, push_to, scratch):
    """
    Attempts to build container images for all of the distgit repositories
    in a group. If an image has already been built, it will be treated as
    a successful operation.

    If docker registries as specified, this action will push resultant
    images to those mirrors as they become available. Note that this should
    be more performant than running images:push since pushes can
    be performed in parallel with other images building.

    Tips on using custom --repo.
    1. Upload a .repo file (it must end in .repo) with your desired yum repos enabled
       into an internal location OSBS can reach like gerrit.
    2. Specify the raw URL to this file for the build.
    3. You will probably want to use --scratch since it is unlikely you want your
        custom build tagged.
    """
    # Initialize all distgit directories before trying to build. This is to
    # ensure all build locks are acquired before the builds start and for
    # clarity in the logs.
    runtime.initialize(clone_distgits=True)

    items = [m.distgit_repo() for m in runtime.image_metas()]
    if not items:
        runtime.logger.info("No images found. Check the arguments.")
        exit(1)

    # Without one of these two arguments, brew would not enable any repos.
    if not repo_type and not repo:
        runtime.logger.info("No repos specified. --repo-type or --repo is required.")
        exit(1)

    results = runtime.parallel_exec(
        lambda (dgr, terminate_event): dgr.build_container(
            odcs, repo_type, repo, push_to_defaults, additional_registries=push_to,
            terminate_event=terminate_event, scratch=scratch),
        items)
    results = results.get()

    try:
        print_build_metrics(runtime)
    except:
        # Never kill a build because of bad logic in metrics
        traceback.print_exc()
        runtime.logger.error("Error trying to show build metrics")

    failed = [m.distgit_key for m, r in zip(runtime.image_metas(), results) if not r]
    if failed:
        runtime.logger.error("\n".join(["Build/push failures:"] + sorted(failed)))
        exit(1)

    # Push all late images
    for image in runtime.image_metas():
        image.distgit_repo().push_image([], push_to_defaults, additional_registries=push_to, push_late=True)


@cli.command("images:push", short_help="Push the most recently built images to mirrors.")
@click.option('--tag', default=[], metavar="PUSH_TAG", multiple=True,
              help='Push to registry using these tags instead of default set.')
@click.option("--version-release", default=None, metavar="VERSION-RELEASE",
              help="Specify an exact version to pull/push (e.g. 'v3.9.31-1' ; default is latest built).")
@click.option('--to-defaults', default=False, is_flag=True, help='Push to default registries.')
@click.option('--late-only', default=False, is_flag=True, help='Push only "late" images.')
@click.option("--to", default=[], metavar="REGISTRY", multiple=True,
              help="Registry to push to when image build completes.  [multiple]")
@click.option('--dry-run', default=False, is_flag=True, help='Only print tag/push operations which would have occurred.')
@pass_runtime
def images_push(runtime, tag, version_release, to_defaults, late_only, to, dry_run):
    """
    Each distgit repository will be cloned and the version and release information
    will be extracted. That information will be used to determine the most recently
    built image associated with the distgit repository.

    An attempt will be made to pull that image and push it to one or more
    docker registries specified on the command line.
    """

    additional_registries = list(to)  # In case we get a tuple

    if to_defaults is False and len(additional_registries) == 0:
        click.echo("You need specify at least one destination registry.")
        exit(1)

    runtime.initialize()

    version_release_tuple = None

    if version_release:
        version_release_tuple = version_release.split('-')
        click.echo('Setting up to push: version={} release={}'.format(version_release_tuple[0], version_release_tuple[1]))

    # late-only is useful if we are resuming a partial build in which not all images
    # can be built/pushed. Calling images:push can end up hitting the same
    # push error, so, without late-only, there is no way to push "late" images and
    # deliver the partial build's last images.
    if not late_only:
        # Allow all non-late push operations to be attempted and track failures
        # with this list. Since "late" images are used as a marker for success,
        # don't push them if there are any preceding errors.
        # This error tolerance is useful primarily in synching images that our
        # team does not build but which should be kept up to date in the
        # operations registry.
        failed = []
        # Push early images

        items = runtime.image_metas()
        results = runtime.parallel_exec(
            lambda (img, terminate_event):
                img.distgit_repo().push_image(tag, to_defaults, additional_registries,
                                              version_release_tuple=version_release_tuple, dry_run=dry_run),
                    items,
                    n_threads=4
                )
        results = results.get()

        failed = [m.distgit_key for m, r in zip(items, results) if not r]
        if failed:
            runtime.logger.error("\n".join(["Push failures:"] + sorted(failed)))
            exit(1)

    # Push all late images
    for image in runtime.image_metas():
        # Check if actually a late image to prevent cloning all distgit on --late-only
        if image.config.push.late is True:
            image.distgit_repo().push_image(tag, to_defaults, additional_registries,
                                            version_release_tuple=version_release_tuple,
                                            push_late=True, dry_run=dry_run)


@cli.command("images:pull", short_help="Pull latest images from pulp")
@pass_runtime
def images_pull_image(runtime):
    """
    Pulls latest images from pull, fetching the dockerfiles from cgit to
    determine the version/release.
    """
    runtime.initialize(clone_distgits=True)
    for image in runtime.image_metas():
        image.pull_image()


@cli.command("images:scan-for-cves", short_help="Scan images with openscap")
@pass_runtime
def images_scan_for_cves(runtime):
    """
    Pulls images and scans them for CVEs using `atomic scan` and `openscap`.
    """
    runtime.initialize(clone_distgits=True)
    image_urls = [x.pull_url() for x in runtime.image_metas()]
    for image_url in image_urls:
        pull_image(image_url, runtime.logger)
    subprocess.check_call(["atomic", "scan"] + image_urls)


@cli.command("images:print", short_help="Print data from each distgit.")
@click.option(
    "--short", default=False, is_flag=True,
    help="Suppress all output other than the data itself")
@click.option('--show-non-release', default=False, is_flag=True,
              help='Include images which have been marked as non-release.')
@click.option('--show-base-only', default=False, is_flag=True,
              help='Include images which have been marked as base images.')
@click.argument("pattern", default="{build}", nargs=1)
@pass_runtime
def images_print(runtime, short, show_non_release, show_base_only, pattern):
    """
    Prints data from each distgit. The pattern specified should be a string
    with replacement fields:

    \b
    {type} - The type of the distgit (e.g. rpms)
    {name} - The name of the distgit repository (e.g. openshift-enterprise)
    {component} - The component identified in the Dockerfile
    {image} - The image name in the Dockerfile
    {version} - The version field in the Dockerfile
    {release} - The release field in the Dockerfile
    {build} - Shorthand for {component}-{version}-{release} (e.g. container-engine-v3.6.173.0.25-1)
    {repository} - Shorthand for {image}:{version}-{release}
    {lf} - Line feed

    If pattern contains no braces, it will be wrapped with them automatically. For example:
    "build" will be treated as "{build}"
    """

    runtime.initialize(clone_distgits=False)

    # If user omitted braces, add them.
    if "{" not in pattern:
        pattern = "{%s}" % pattern.strip()

    count = 0
    if short:
        echo_verbose = lambda _: None
    else:
        echo_verbose = click.echo

    echo_verbose("")
    echo_verbose("------------------------------------------")

    non_release_images = runtime.group_config.non_release.images
    if non_release_images is Missing:
        non_release_images = []

    if not show_non_release:
        images = [i for i in runtime.image_metas() if i.distgit_key not in non_release_images]
    else:
        images = list(runtime.image_metas())

    for image in images:
        click.echo(image.in_group_config_path)
        count += 1
        continue

        # skip base images unless requested
        if image.base_only and not show_base_only:
            continue

        dfp = DockerfileParser(path=runtime.working_dir)
        try:
            dfp.content = image.fetch_cgit_file("Dockerfile")
        except Exception:
            click.echo("Error reading Dockerfile from distgit: {}".format(image.distgit_key))
            raise

        version = dfp.labels["version"]

        s = pattern
        s = s.replace("{build}", "{component}-{version}-{release}")
        s = s.replace("{repository}", "{image}:{version}-{release}")
        s = s.replace("{namespace}", image.namespace)
        s = s.replace("{name}", image.name)
        s = s.replace("{component}", image.get_component_name())
        s = s.replace("{image}", dfp.labels["name"])
        s = s.replace("{version}", version)
        s = s.replace("{lf}", "\n")

        release_query_needed = '{release}' in s or '{pushes}' in s

        # Since querying release takes time, check before executing replace
        release = ''
        if release_query_needed:
            _, _, release = image.get_latest_build_info()

        s = s.replace("{release}", release)

        pushes_formatted = ''
        for push_name in image.get_default_push_names():
            pushes_formatted += '\t{} : [{}]\n'.format(push_name, ', '.join(image.get_default_push_tags(version, release)))

        if pushes_formatted is '':
            pushes_formatted = "(None)"

        s = s.replace("{pushes}", '{}\n'.format(pushes_formatted))

        if "{" in s:
            raise IOError("Unrecognized fields remaining in pattern: %s" % s)

        click.echo(s)
        count += 1

    echo_verbose("------------------------------------------")
    echo_verbose("{} images".format(count))

    # If non-release images are being suppressed, let the user know
    if not show_non_release and non_release_images:
        echo_verbose("\nThe following {} non-release images were excluded; use --show-non-release to include them:".format(
            len(non_release_images)))
        for image in non_release_images:
            echo_verbose("    {}".format(image))


@cli.command("images:print-config-template", short_help="Create template package yaml from distgit Dockerfile.")
@click.argument("url", nargs=1)
def distgit_config_template(url):
    """
    Pulls the specified URL (to a Dockerfile in distgit) and prints the boilerplate
    for a config yaml for the image.
    """

    f = urllib.urlopen(url)
    if f.code != 200:
        click.echo("Error fetching {}: {}".format(url, f.code), err=True)
        exit(1)

    dfp = DockerfileParser()
    dfp.content = f.read()

    if "cgit/rpms/" in url:
        type = "rpms"
    elif "cgit/containers/" in url:
        type = "containers"
    elif "cgit/apbs/" in url:
        type = "apbs"
    else:
        raise IOError("oit does not yet support that distgit repo type")

    config = {
        "repo": {
            "type": type,
        },
        "name": dfp.labels['name'],
        "from": {
            "image": dfp.baseimage
        },
        "labels": {},
        "owners": []
    }

    branch = url[url.index("?h=") + 3:]

    if "Architecture" in dfp.labels:
        dfp.labels["architecture"] = dfp.labels["Architecture"]

    component = dfp.labels.get("com.redhat.component", dfp.labels.get("BZComponent", None))

    if component is not None:
        config["repo"]["component"] = component

    managed_labels = [
        'vendor',
        'License',
        'architecture',
        'io.k8s.display-name',
        'io.k8s.description',
        'io.openshift.tags'
    ]

    for ml in managed_labels:
        if ml in dfp.labels:
            config["labels"][ml] = dfp.labels[ml]

    click.echo("---")
    click.echo("# populated from branch: {}".format(branch))
    yaml.safe_dump(config, sys.stdout, indent=2, default_flow_style=False)


@cli.command(
    "completion", short_help="Output bash completion function",
    help="""\
Generate a bash function for auto-completion on the command line. The output
is formatted so that it can be fed to the shell's `source` command directly:

    $ source <(/path/to/oit completion)
""")
def completion():
    basename = os.path.basename(sys.argv[0])
    click.echo("""\
_oit_completion() {
    local cmd word prev mdir group types
    cmd=$1; word=$2; prev=$3
    set -- "${COMP_WORDS[@]}"
    mdir=groups; group=
    while [[ "${#}" -gt 0 ]]; do
        case "${1}" in
            --metadata-dir) mdir=${2}; shift; shift; ;;
            --group) group=${2}; shift; shift; ;;
            *) shift;
        esac
    done
    case "${prev}" in
        -i|--images) types=images ;;
        -r|--rpms) types=rpms ;;
        -x|--exclude) types='images rpms' ;;
        --group)
            if [ -d "${mdir}" ]; then
                COMPREPLY=( $(compgen -W "$(ls "${mdir}")" -- "${word}") )
            fi
            return ;;
        *) COMPREPLY=( $(env \
                COMP_WORDS="${COMP_WORDS[*]}" \
                COMP_CWORD=$COMP_CWORD \
                _%s_COMPLETE=complete "${cmd}") )
            return ;;
    esac
    group=$(echo "${group}" | tr , '\n')
    group=$( \
        cd "${mdir}" \
        && for g in ${group:-*}; do \
            for t in ${types}; do \
                if [[ -d "${g}/${t}" ]]; then \
                    basename --multiple --suffix .yml "${g}/${t}"/*; \
                fi \
            done \
        done \
        | sort -u)
    COMPREPLY=( $(compgen -W "${group}" -- "${word}") )
}
complete -F _oit_completion -o default %s
""" % (basename.replace("-", "_").upper(), basename))


@cli.command("images:query-rpm-version", short_help="Find the OCP version from the atomic-openshift RPM")
@click.option("--repo-type", metavar="REPO_TYPE", envvar="OIT_IMAGES_REPO_TYPE",
              default="unsigned",
              help="Repo group to scan for the RPM (e.g. signed, unsigned). env: OIT_IMAGES_REPO_TYPE")
@pass_runtime
def query_rpm_version(runtime, repo_type):
    """
    Retrieve the version number of the atomic-openshift RPM in the indicated
    repository. This is the version number that will be applied to new images
    created from this build.
    """
    runtime.initialize(clone_distgits=False)

    version = runtime.auto_version(repo_type)
    click.echo("version: {}".format(version))


@cli.command("cleanup", short_help="Cleanup the OIT environment")
@pass_runtime
def cleanup(runtime):
    """
    Cleanup the OIT working environment.
    Currently this just clears out the working dir content
    """

    runtime.initialize(no_group=True)

    runtime.logger.info('Clearing out {}'.format(runtime.working_dir))
    if os.path.isdir(runtime.working_dir):
        shutil.rmtree(runtime.working_dir, ignore_errors=True)
        os.makedirs(runtime.working_dir)  # rmtree deletes the directory itself. recreate


option_config_commit_msg = click.option("--message", "-m", metavar='MSG', help="Commit message for config change.", default=None)


# config:* commands are a special beast and
# requires the same non-standard runtime options
CONFIG_RUNTIME_OPTS = {
    'mode': 'both',           # config wants it all
    'clone_distgits': False,  # no need, just doing config
    'clone_source': False,    # no need, just doing config
    'disabled': True          # show all, including disabled/wip
}


# Normally runtime only runs in one mode as you never do
# rpm AND image operations at once. This is not so with config
# functions. This intelligently chooses modes for these only
def _fix_runtime_mode(runtime):
    mode = 'both'
    if runtime.rpms and not runtime.images:
        mode = 'rpms'
    elif runtime.images and not runtime.rpms:
        mode = 'images'

    CONFIG_RUNTIME_OPTS['mode'] = mode


@cli.command("config:commit", help="Commit pending changes from config:new")
@option_config_commit_msg
@click.option('--push/--no-push', default=False, is_flag=True,
              help='Push changes back to config repo')
@pass_runtime
def config_commit(runtime, message, push):
    """
    Commit outstanding metadata config changes
    """
    _fix_runtime_mode(runtime)
    runtime.initialize(no_group=True, **CONFIG_RUNTIME_OPTS)
    config = mdc(runtime)
    config.sanitize_new_config()
    config.commit(message)
    if push:
        config.push()


@cli.command("config:push", help="Push all pending changes to config repo")
@pass_runtime
def config_push(runtime):
    """
    Push changes back to config repo.
    Will of course fail if user does not have write access.
    """
    _fix_runtime_mode(runtime)
    runtime.initialize(no_group=True, **CONFIG_RUNTIME_OPTS)
    config = mdc(runtime)
    config.push()


@cli.command("config:mode", short_help="Update config(s) mode. enable|disable|wip")
@click.argument("mode", nargs=1, metavar="MODE", type=click.Choice(metadata.CONFIG_MODES))  # new mode value
@click.option('--push/--no-push', default=False, is_flag=True,
              help='Push changes back to config repo')
@option_config_commit_msg
@pass_runtime
def config_mode(runtime, mode, push, message):
    """Update [MODE] of given config(s) to one of:
    - enable: Normal operation
    - disable: Will not be used unless explicitly specified
    - wip: Same as `disable` plus affected by --wip flag

    Filtering of configs is based on usage of the following global options:
    --group, --images/-i, --rpms/-r

    See `oit.py --help` for more.

    Usage:

    $ oit.py --group=openshift-4.0 -i aos3-installation config:mode [MODE]

    Where [MODE] is one of enable, disable, or wip.

    Multiple configs may be specified and updated at once.

    Commit message will default to stating mode change unless --message given.
    If --push not given must use config:push after.
    """
    _fix_runtime_mode(runtime)
    if not runtime.wip and CONFIG_RUNTIME_OPTS['mode'] == 'both':
        click.echo('Updating all mode for all configs in group is not allowed! Please specifiy configs directly.')
        sys.exit(1)
    runtime.initialize(**CONFIG_RUNTIME_OPTS)
    config = mdc(runtime)
    config.update('mode', mode)
    if not message:
        message = 'Updating [mode] to "{}"'.format(mode)
    config.commit(message)

    if push:
        config.push()


@cli.command("config:print", short_help="View config for given images / rpms")
@click.option("-n", "--name-only", default=False, is_flag=True, multiple=True,
              help="Just print name of matched configs. Overrides --key")
@click.option("--key", help="Specific key in config to print", default=None)
@pass_runtime
def config_print(runtime, key, name_only):
    """Print name, sub-key, or entire config

    Filtering of configs is based on usage of the following global options:
    --group, --images/-i, --rpms/-r

    See `oit.py --help` for more.

    Examples:

    Print all configs in group:

        $ oit.py --group=openshift-4.0 config:print

    Print single config in group:

        $ oit.py --group=openshift-4.0 -i aos3-installation config:print

    Print `owners` key from all configs in group:

        $ oit.py --group=openshift-4.0 config:print --key owners

    Print only names of configs in group:

        $ oit.py --group=openshift-4.0 config:print --name-only
    """
    _fix_runtime_mode(runtime)
    runtime.initialize(**CONFIG_RUNTIME_OPTS)
    config = mdc(runtime)
    config.config_print(key, name_only)


@cli.command("config:new", short_help="Add new config. Follow up with config:commit")
@click.argument("new_type", nargs=1, metavar="TYPE", type=click.Choice(['image', 'rpm']))
@click.argument("name", nargs=1, metavar="NAME")
@pass_runtime
def config_new(runtime, new_type, name):
    """Copy a TYPE kind of template config (one of 'image' or 'rpm') into correct place naming the
    new component config after NAME. Report that new config file path
    for later editing.

    Filtering of configs is based on usage of the following global options:
    --group, --images/-i, --rpms/-r

    See `oit.py --help` for more.

    Examples:

    Add a new 'image' TYPE of config with the NAME 'megafrobber'

        $ oit.py --group=openshift-4.0 config:new image megafrobber

    Commit that new change to git:

        $ oit.py --group=openshift-4.0 config:commit
    """

    runtime.initialize(**CONFIG_RUNTIME_OPTS)
    config = mdc(runtime)
    config.new(new_type, name)

    click.echo('Remember to use config:commit after the new config is complete')


if __name__ == '__main__':
    cli(obj={})
