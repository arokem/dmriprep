"""dMRIPrep base processing workflows."""
from .. import config
import sys
import os
from copy import deepcopy

from nipype.pipeline import engine as pe
from nipype.interfaces import utility as niu

from niworkflows.engine.workflows import LiterateWorkflow as Workflow
from niworkflows.interfaces.bids import BIDSInfo, BIDSFreeSurferDir
from niworkflows.utils.misc import fix_multi_T1w_source_name
from niworkflows.utils.spaces import Reference
from smriprep.workflows.anatomical import init_anat_preproc_wf

from ..interfaces import DerivativesDataSink, BIDSDataGrabber
from ..interfaces.reports import SubjectSummary, AboutSummary
from ..utils.bids import collect_data


def init_dmriprep_wf():
    """
    Create the base workflow.

    This workflow organizes the execution of *dMRIPrep*, with a sub-workflow for
    each subject. If FreeSurfer's recon-all is to be run, a FreeSurfer derivatives folder is
    created and populated with any needed template subjects.

    Workflow Graph
        .. workflow::
            :graph2use: orig
            :simple_form: yes

            from dmriprep.config.testing import mock_config
            from dmriprep.workflows.base import init_dmriprep_wf
            with mock_config():
                wf = init_dmriprep_wf()

    """
    dmriprep_wf = Workflow(name="dmriprep_wf")
    dmriprep_wf.base_dir = config.execution.work_dir

    freesurfer = config.workflow.run_reconall
    if freesurfer:
        fsdir = pe.Node(
            BIDSFreeSurferDir(
                derivatives=config.execution.output_dir,
                freesurfer_home=os.getenv("FREESURFER_HOME"),
                spaces=config.workflow.spaces.get_fs_spaces(),
            ),
            name=f"fsdir_run_{config.execution.run_uuid.replace('-', '_')}",
            run_without_submitting=True,
        )
        if config.execution.fs_subjects_dir is not None:
            fsdir.inputs.subjects_dir = str(config.execution.fs_subjects_dir.absolute())

    for subject_id in config.execution.participant_label:
        single_subject_wf = init_single_subject_wf(subject_id)

        single_subject_wf.config["execution"]["crashdump_dir"] = str(
            config.execution.output_dir
            / "dmriprep"
            / f"sub-{subject_id}"
            / "log"
            / config.execution.run_uuid
        )

        for node in single_subject_wf._get_all_nodes():
            node.config = deepcopy(single_subject_wf.config)
        if freesurfer:
            dmriprep_wf.connect(
                fsdir, "subjects_dir", single_subject_wf, "fsinputnode.subjects_dir"
            )
        else:
            dmriprep_wf.add_nodes([single_subject_wf])

        # Dump a copy of the config file into the log directory
        log_dir = (
            config.execution.output_dir
            / "dmriprep"
            / f"sub-{subject_id}"
            / "log"
            / config.execution.run_uuid
        )
        log_dir.mkdir(exist_ok=True, parents=True)
        config.to_filename(log_dir / "dmriprep.toml")

    return dmriprep_wf


def init_single_subject_wf(subject_id):
    """
    Set-up the preprocessing pipeline for a single subject.

    It collects and reports information about the subject, and prepares
    sub-workflows to perform anatomical and diffusion MRI preprocessing.

    Anatomical preprocessing is performed in a single workflow, regardless of
    the number of sessions.
    Diffusion MRI preprocessing is performed using a separate workflow for
    a full :abbr:`DWI (diffusion weighted imaging)` *entity*.
    A DWI *entity* may comprehend one or several runs (for instance, two
    opposed :abbr:`PE (phase-encoding)` directions.

    Workflow Graph
        .. workflow::
            :graph2use: orig
            :simple_form: yes

            from dmriprep.config.testing import mock_config
            from dmriprep.workflows.base import init_single_subject_wf
            with mock_config():
                wf = init_single_subject_wf("THP0005")

    Parameters
    ----------
    subject_id : str
        List of subject labels

    Inputs
    ------
    subjects_dir : os.pathlike
        FreeSurfer's ``$SUBJECTS_DIR``

    """
    from ..utils.misc import sub_prefix as _prefix

    name = f"single_subject_{subject_id}_wf"
    subject_data = collect_data(config.execution.layout, subject_id)[0]

    if "flair" in config.workflow.ignore:
        subject_data["flair"] = []
    if "t2w" in config.workflow.ignore:
        subject_data["t2w"] = []

    anat_only = config.workflow.anat_only

    # Make sure we always go through these two checks
    if not anat_only and not subject_data["dwi"]:
        raise Exception(
            f"No DWI data found for participant {subject_id}. "
            "All workflows require DWI images."
        )

    if not subject_data["t1w"]:
        raise Exception(
            f"No T1w images found for participant {subject_id}. "
            "All workflows require T1w images."
        )

    workflow = Workflow(name=name)
    workflow.__desc__ = f"""
Results included in this manuscript come from preprocessing
performed using *dMRIPrep* {config.environment.version}
(@dmriprep; RRID:SCR_017412),
which is based on *Nipype* {config.environment.nipype_version}
(@nipype1; @nipype2; RRID:SCR_002502).

"""
    workflow.__postdesc__ = """

For more details of the pipeline, see [the section corresponding
to workflows in *dMRIPrep*'s documentation]\
(https://nipreps.github.io/dmriprep/master/workflows.html \
"dMRIPrep's documentation").


### Copyright Waiver

The above boilerplate text was automatically generated by dMRIPrep
with the express intention that users should copy and paste this
text into their manuscripts *unchanged*.
It is released under the [CC0]\
(https://creativecommons.org/publicdomain/zero/1.0/) license.

### References

"""
    spaces = config.workflow.spaces
    output_dir = config.execution.output_dir

    fsinputnode = pe.Node(
        niu.IdentityInterface(fields=["subjects_dir"]), name="fsinputnode"
    )

    bidssrc = pe.Node(
        BIDSDataGrabber(subject_data=subject_data, anat_only=anat_only), name="bidssrc"
    )

    bids_info = pe.Node(
        BIDSInfo(bids_dir=config.execution.bids_dir, bids_validate=False),
        name="bids_info",
    )

    summary = pe.Node(
        SubjectSummary(
            std_spaces=spaces.get_spaces(nonstandard=False),
            nstd_spaces=spaces.get_spaces(standard=False),
        ),
        name="summary",
        run_without_submitting=True,
    )

    about = pe.Node(
        AboutSummary(version=config.environment.version, command=" ".join(sys.argv)),
        name="about",
        run_without_submitting=True,
    )

    ds_report_summary = pe.Node(
        DerivativesDataSink(
            base_directory=str(output_dir), desc="summary", datatype="figures"
        ),
        name="ds_report_summary",
        run_without_submitting=True,
    )

    ds_report_about = pe.Node(
        DerivativesDataSink(
            base_directory=str(output_dir), desc="about", datatype="figures"
        ),
        name="ds_report_about",
        run_without_submitting=True,
    )

    anat_derivatives = config.execution.anat_derivatives
    if anat_derivatives:
        from smriprep.utils.bids import collect_derivatives

        std_spaces = spaces.get_spaces(nonstandard=False, dim=(3,))
        anat_derivatives = collect_derivatives(
            anat_derivatives.absolute(),
            subject_id,
            std_spaces,
            config.workflow.run_reconall,
        )

    # Preprocessing of T1w (includes registration to MNI)
    anat_preproc_wf = init_anat_preproc_wf(
        bids_root=str(config.execution.bids_dir),
        debug=config.execution.debug is True,
        existing_derivatives=anat_derivatives,
        freesurfer=config.workflow.run_reconall,
        hires=config.workflow.hires,
        longitudinal=config.workflow.longitudinal,
        omp_nthreads=config.nipype.omp_nthreads,
        output_dir=str(output_dir),
        skull_strip_fixed_seed=config.workflow.skull_strip_fixed_seed,
        skull_strip_mode="force",
        skull_strip_template=Reference.from_string(
            config.workflow.skull_strip_template
        )[0],
        spaces=spaces,
        t1w=subject_data["t1w"],
    )

    # fmt:off
    workflow.connect([
        (fsinputnode, anat_preproc_wf, [("subjects_dir", "inputnode.subjects_dir")]),
        (bidssrc, bids_info, [(("t1w", fix_multi_T1w_source_name), "in_file")]),
        (fsinputnode, summary, [("subjects_dir", "subjects_dir")]),
        (bidssrc, summary, [("t1w", "t1w"), ("t2w", "t2w"), ("dwi", "dwi")]),
        (bids_info, summary, [("subject", "subject_id")]),
        (bids_info, anat_preproc_wf, [(("subject", _prefix), "inputnode.subject_id")]),
        (bidssrc, anat_preproc_wf, [
            ("t1w", "inputnode.t1w"),
            ("t2w", "inputnode.t2w"),
            ("roi", "inputnode.roi"),
            ("flair", "inputnode.flair"),
        ]),
        (bidssrc, ds_report_summary, [
            (("t1w", fix_multi_T1w_source_name), "source_file"),
        ]),
        (summary, ds_report_summary, [("out_report", "in_file")]),
        (bidssrc, ds_report_about, [
            (("t1w", fix_multi_T1w_source_name), "source_file")
        ]),
        (about, ds_report_about, [("out_report", "in_file")]),
    ])
    # fmt:off
    # Overwrite ``out_path_base`` of smriprep's DataSinks
    for node in workflow.list_node_names():
        if node.split(".")[-1].startswith("ds_"):
            workflow.get_node(node).interface.out_path_base = "dmriprep"

    if anat_only:
        return workflow

    from .dwi.base import init_dwi_preproc_wf
    # Append the dMRI section to the existing anatomical excerpt
    # That way we do not need to stream down the number of DWI datasets
    anat_preproc_wf.__postdesc__ = (
        (anat_preproc_wf.__postdesc__ or "")
        + f"""
Diffusion data preprocessing

: For each of the {len(subject_data["dwi"])} DWI scans found per subject
 (across all sessions), the gradient table was vetted and converted into the *RASb*
format (i.e., given in RAS+ scanner coordinates, normalized b-vectors and scaled b-values),
and a *b=0* average for reference to the subsequent steps of preprocessing was calculated.
"""
    )

    for dwi_file in subject_data["dwi"]:
        dwi_preproc_wf = init_dwi_preproc_wf(dwi_file)

        # fmt: off
        workflow.connect([
            (anat_preproc_wf, dwi_preproc_wf,
             [("outputnode.t1w_preproc", "inputnode.t1w_preproc"),
              ("outputnode.t1w_mask", "inputnode.t1w_mask"),
              ("outputnode.t1w_dseg", "inputnode.t1w_dseg"),
              ("outputnode.t1w_aseg", "inputnode.t1w_aseg"),
              ("outputnode.t1w_aparc", "inputnode.t1w_aparc"),
              ("outputnode.t1w_tpms", "inputnode.t1w_tpms"),
              ("outputnode.template", "inputnode.template"),
              ("outputnode.anat2std_xfm", "inputnode.anat2std_xfm"),
              ("outputnode.std2anat_xfm", "inputnode.std2anat_xfm"),
              # Undefined if --fs-no-reconall, but this is safe
              ("outputnode.subjects_dir", "inputnode.subjects_dir"),
              ("outputnode.t1w2fsnative_xfm", "inputnode.t1w2fsnative_xfm"),
              ("outputnode.fsnative2t1w_xfm", "inputnode.fsnative2t1w_xfm")]),
            (bids_info, dwi_preproc_wf, [("subject", "inputnode.subject_id")]),
        ])
        # fmt: on

    if "fieldmap" in config.workflow.ignore:
        return workflow

    from sdcflows import fieldmaps as fm
    from sdcflows.utils.wrangler import find_estimators
    from sdcflows.workflows.base import init_fmap_preproc_wf

    # SDCFlows connection
    # Step 1: Run basic heuristics to identify available data for fieldmap estimation
    estimators = find_estimators(config.execution.layout)

    if not estimators and config.workflow.use_syn:  # Add fieldmap-less estimators
        # estimators = [fm.FieldmapEstimation()]
        raise NotImplementedError

    # Step 2: Manually add further estimators (e.g., fieldmap-less)
    fmap_wf = init_fmap_preproc_wf(
        debug=config.execution.debug,
        estimators=estimators,
        omp_nthreads=config.nipype.omp_nthreads,
        output_dir=str(output_dir),
        subject=subject_id,
    )

    # Overwrite ``out_path_base`` of sdcflows's DataSinks
    for node in fmap_wf.list_node_names():
        if node.split(".")[-1].startswith("ds_"):
            fmap_wf.get_node(node).interface.out_path_base = "dmriprep"
    workflow.add_nodes([fmap_wf])

    # Step 3: Manually connect PEPOLAR
    for estimator in estimators:
        if estimator.method != fm.EstimatorType.PEPOLAR:
            continue

        suffices = set(s.suffix for s in estimator.sources)

        if sorted(suffices) == ["epi"]:
            getattr(fmap_wf.inputs, f"in_{estimator.bids_id}").in_data = [
                str(s.path) for s in estimator.sources
            ]
            getattr(fmap_wf.inputs, f"in_{estimator.bids_id}").metadata = [
                s.metadata for s in estimator.sources
            ]
        else:
            raise NotImplementedError
            # from niworkflows.interfaces.utility import KeySelect
            # est_id = estimator.bids_id
            # fmap_select = pe.MapNode(
            #     KeySelect(fields=["metadata", "dwi_reference", "dwi_mask", "gradients_rasb",]),
            #     name=f"fmap_select_{est_id}",
            #     run_without_submitting=True,
            #     iterfields=["key"]
            # )
            # fmap_select.inputs.key = [
            #     str(s.path) for s in estimator.sources if s.suffix in ("epi", "dwi", "sbref")
            # ]
            # # fmt:off
            # workflow.connect([
            #     (referencenode, fmap_select, [("dwi_file", "keys"),
            #                                   ("metadata", "metadata"),
            #                                   ("dwi_reference", "dwi_reference"),
            #                                   ("gradients_rasb", "gradients_rasb")]),
            # ])
            # # fmt:on

    return workflow
