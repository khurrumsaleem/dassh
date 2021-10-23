########################################################################
# Copyright 2021, UChicago Argonne, LLC
#
# Licensed under the BSD-3 License (the "License"); you may not use
# this file except in compliance with the License. You may obtain a
# copy of the License at
#
#     https://opensource.org/licenses/BSD-3-Clause
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied. See the License for the specific language governing
# permissions and limitations under the License.
########################################################################
"""
date: 2021-10-22
author: matz
Main DASSH calculation procedure
"""
########################################################################
import os
import sys
import dassh
import argparse
import cProfile
_log_info = 20  # logging levels must be int


def main(args=None):
    """Perform temperature sweep in DASSH"""
    # Parse command line arguments to DASSH
    parser = argparse.ArgumentParser(description='Process DASSH cmd')
    parser.add_argument('inputfile',
                        metavar='inputfile',
                        help='The input file to run with DASSH')
    parser.add_argument('--verbose',
                        action='store_true',
                        help='Verbose; print summary with each axial step')
    parser.add_argument('--save_reactor',
                        action='store_true',
                        help='Save DASSH Reactor object after sweep')
    parser.add_argument('--profile',
                        action='store_true',
                        help='Profile the execution of DASSH')
    parser.add_argument('--no_power_calc',
                        action='store_false',
                        help='Skip VARPOW calculation if done previously')
    args = parser.parse_args(args)

    # Enable the profiler, if desired
    if args.profile:
        pr = cProfile.Profile()
        pr.enable()

    # Initiate logger
    print(dassh._ascii._ascii_title)
    in_path = os.path.split(args.inputfile)[0]
    dassh_logger = dassh.logged_class.init_root_logger(in_path, 'dassh')

    # Pre-processing
    # Read input file and set up DASSH input object
    dassh_logger.log(_log_info, f'Reading input: {args.inputfile}')
    dassh_input = dassh.DASSH_Input(args.inputfile)

    # DASSH calculation without orificing optimization
    if dassh_input.data['Orificing'] is False:
        arg_dict = {'save_reactor': args.save_reactor,
                    'verbose': args.verbose,
                    'no_power_calc': args.no_power_calc}
        run_dassh(dassh_input, dassh_logger, arg_dict)

    # Orificing optimization with DASSH
    else:
        # dassh.orificing.optimize(dassh_input, dassh_logger)
        orifice_obj = dassh.orificing.Orificing(
            dassh_input, dassh_logger)
        orifice_obj.optimize()

    # Finish the calculation
    dassh_logger.log(_log_info, 'DASSH execution complete')
    # Print/dump profiler results
    if args.profile:
        pr.disable()
        pr.dump_stats('dassh_profile.out')


def run_dassh(dassh_input, dassh_logger, args):
    """Run DASSH without orificing optimization"""
    # For each timestep in the DASSH input, create the necessary DASSH
    # DASSH objects, run DASSH, and process the results
    need_subdir = False
    if dassh_input.timepoints > 1:
        need_subdir = True
    for i in range(dassh_input.timepoints):
        working_dir = None
        if need_subdir:
            # Only log info about timestep if you have multiple
            dassh_logger.log(_log_info, f'Timestep {i + 1}')
            working_dir = os.path.join(
                dassh_input.path, f'timestep_{i + 1}')
        # Set up working dirs, run DASSH, write output, make plots
        _run_dassh(dassh_input, dassh_logger, args, i, working_dir)


def _run_dassh(dassh_inp, dassh_log, args, timestep, wdir, link=None):
    """Run DASSH for a single timestep

    Parameters
    ----------
    dassh_inp : DASSH_Input object
        Base DASSH input class
    dassh_log : LoggedClass object
        Keep logging while in this function
    args : dict
        Various args for instantiating DASSH objects
    timestep : int
        Timestep for which to run DASSH
    wdir : str
        Path to working directory for this timestep
    link : str (optional)
        Try to link VARPOW output files from another path
        Avoids repetitive calcs in orificing optimization
        (default = None; run VARPOW as usual)

    """
    # Try to link VARPOW output from another source. If it doesn't
    # exist or work, just rerun VARPOW.
    if link is not None:
        files_linked = 0
        for f in ['varpow_MatPower.out',
                  'varpow_MonoExp.out',
                  'VARPOW.out']:
            src = os.path.join(link, f)
            dest = os.path.join(wdir, f)
            if os.path.exists(src):
                os.symlink(src, dest)
                files_linked += 1
            else:
                break
        # If all VARPOW files were linked, can skip VARPOW calculation
        if files_linked == 3:
            args['no_power_calc'] = False  # if linked, skip VARPOW
        else:
            args['no_power_calc'] = True

    # Initialize the Reactor object
    reactor = dassh.Reactor(dassh_inp,
                            calc_power=args['no_power_calc'],
                            path=wdir,
                            timestep=timestep,
                            write_output=True)
    # Perform the sweep
    dassh_log.log(_log_info, 'Performing temperature sweep...')
    reactor.temperature_sweep(verbose=args['verbose'])

    # Post-processing: write output, save reactor if desired
    dassh_log.log(_log_info, 'Temperature sweep complete')
    if args['save_reactor'] and sys.version_info >= (3, 7):
        reactor.save()
    elif dassh_inp.data['Plot']:
        reactor.save()  # just in case plotting fails
    else:
        pass
    dassh_log.log(_log_info, 'Output written')

    # Post-processing: generate figures, if desired
    if ('Plot' in dassh_inp.data.keys()
            and len(dassh_inp.data['Plot']) > 0):
        dassh_log.log(_log_info, 'Generating figures')
        dassh.plot.plot_all(dassh_inp, reactor)


def plot():
    """Command-line interface to postprocess DASSH data to make
    matplotlib figures"""
    # Get input file from command line arguments
    parser = argparse.ArgumentParser(description='Process DASSH cmd')
    parser.add_argument('inputfile',
                        metavar='inputfile',
                        help='The input file to run with DASSH')
    args = parser.parse_args()

    # Initiate logger
    print(dassh._ascii._ascii_title)
    in_path = os.path.split(args.inputfile)[0]
    dassh_logger = dassh.logged_class.init_root_logger(in_path,
                                                       'dassh_plot')

    # Check whether Reactor object exists; if so, process with
    # DASSHPlot_Input and get remaining info from Reactor object
    rpath = os.path.join(os.path.abspath(in_path), 'dassh_reactor.pkl')
    if os.path.exists(rpath):
        dassh_logger.log(_log_info, f'Loading DASSH Reactor: {rpath}')
        r = dassh.reactor.load(rpath)
        dassh_logger.log(_log_info, f'Reading input: {args.inputfile}')
        inp = dassh.DASSHPlot_Input(args.inputfile, r)

    # Otherwise, build Reactor object from complete DASSH input
    else:
        dassh_logger.log(_log_info, f'Reading input: {args.inputfile}')
        inp = dassh.DASSH_Input(args.inputfile)
        dassh_logger.log(_log_info, 'Building DASSH Reactor from input')
        r = dassh.Reactor(inp, calc_power=False)

    # Generate figures
    dassh_logger.log(_log_info, 'Generating figures')
    dassh.plot.plot_all(inp, r)
    dassh_logger.log(_log_info, 'DASSH_PLOT execution complete')


if __name__ == '__main__':
    main()
