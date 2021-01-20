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
date: 2020-12-23
author: matz
Object to hold and control DASSH components and execute simulations
"""
########################################################################
import os
import copy
# import h5py
import numpy as np
import subprocess
import logging
import sys
import pickle
import datetime
import time
import multiprocessing as mp

import py4c
import dassh
from dassh.logged_class import LoggedClass


_FUELS = {'metal': {'zr': 1, 'zirconium': 1, 'al': 4, 'aluminum': 4},
          'oxide': 2,
          'nitride': 3}
_COOLANTS = {'na': 1, 'sodium': 1,
             'nak': 2, 'sodium-potassium': 2,
             'pb': 3, 'lead': 3,
             'pb-bi': 4, 'lead-bismuth': 4,
             'lbe': 4, 'lead-bismuth-eutectic': 4,
             'sn': 5, 'tin': 5}


module_logger = logging.getLogger('dassh.reactor')


def load(path):
    """Load a saved Reactor object from a file

    Parameters
    ----------
    path : str
        Path to Reactor object (default file is dassh_reactor.pkl)

    Returns
    -------
    DASSH Reactor object

    """
    with open(path, 'rb') as f:
        obj = pickle.load(f)
    return obj


class Reactor(LoggedClass):
    """Object to hold and control DASSH Assembly and Core objects and
    perform temperature sweep calculations per user input.

    Attributes
    ----------

    Notes
    -----
    in __init__:
    - Calculate core power profile (call method)
    - Instantiate base assemblies
    - Set up assembly list via clone
    - Set up core object
    - Calculate axial constraint

    Also include in this object:
    - Subchannel temperature arrays for core object, each assembly
    - sweep method to perform whole core temperature calculation
    - oriface method to iteratively call sweep method

    """
    def __init__(self, dassh_input, path=None, calc_power=True, **kwargs):
        """Initialize Reactor object for DASSH simulation

        Parameters
        ----------
        dassh_input : DASSH_Input object
            DASSH input from read_input.DASSH_Input

        """
        LoggedClass.__init__(self, 0, 'dassh.reactor.Reactor')

        # Store user options from input/invocation
        self.units = dassh_input.data['Setup']['Units']
        self._setup_options(dassh_input, **kwargs)

        # Store general inputs
        self.inlet_temp = dassh_input.data['Core']['coolant_inlet_temp']
        self.asm_pitch = dassh_input.data['Core']['assembly_pitch']
        if path is None:
            self.path = dassh_input.path
        else:
            self.path = path
            os.makedirs(path, exist_ok=True)

        # Store DASSH materials (already loaded in DASSH_Input)
        self.materials = dassh_input.materials

        # Set up power, obtain axial region boundaries
        self.log('info', 'Setting up power distribution')
        self._setup_power(dassh_input, calc_power)
        self._setup_axial_region_bnds(dassh_input)

        # Set up DASSH Assemblies by first creating templates, then
        # cloning them into each specified position in the core
        self.log('info', 'Generating Assembly objects')
        self._assembly_template_setup(dassh_input)
        asm_power = self._setup_asm_power(dassh_input)
        est_Tout, est_fr = self._setup_asm_bc(dassh_input, asm_power)
        self._setup_asm(dassh_input, asm_power, est_Tout, est_fr)
        self._setup_asm_axial_mesh_req()

        # Set up DASSH Core object; first need to calculate inter-
        # assembly gap flow rate based on total flow rate to the
        # assemblies.
        self.log('info', 'Generating Core object')
        self.flow_rate = self._calculate_total_fr(dassh_input)
        self._core_setup(dassh_input)

        # Report some updates: total power and flow rate
        msg = 'Total power (W): {:.1f}'.format(self.total_power)
        self.log('info', msg)
        msg = 'Total flow rate (kg/s): {:.4f}'.format(self.flow_rate)
        self.log('info', msg)

        # Set up axial mesh
        self._setup_overall_axial_mesh_req()
        self.z, self.dz = self._setup_zpts()
        self.log('info', f'{len(self.z) - 1} axial steps required')
        # Warn if axial steps too small (< 0.5 mm) or too many (> 4k)
        if self.req_dz < 0.0005 or len(self.z) - 1 > 2500:
            msg = ('Your axial step size is very small so this '
                   'problem might take a while to solve;\nConsider '
                   'checking input for flow maldistribution.')
            self.log('warning', msg)

        # Raise warning if est. coolant temp will exceed extreme limit
        self._melt_warning(dassh_input, T_max=1500)

        # Generate general output file
        if self._options['write_output']:
            self.write_summary()

    # def __init__(self, dassh_input, path=None, calc_power=True, **kwargs):
    #     """Initialize Reactor object for DASSH simulation
    #
    #     Parameters
    #     ----------
    #     dassh_input : DASSH_Input object
    #         DASSH input from read_input.DASSH_Input
    #
    #     """
    #     LoggedClass.__init__(self, 0, 'dassh.reactor.Reactor')
    #
    #     # Store user options from input/invocation
    #     self.units = dassh_input.data['Setup']['Units']
    #     self._setup_options()
    #
    #     # Store general inputs
    #     self.inlet_temp = dassh_input.data['Core']['coolant_inlet_temp']
    #     self.asm_pitch = dassh_input.data['Core']['assembly_pitch']
    #     if path is None:
    #         self.path = dassh_input.path
    #     else:
    #         self.path = path
    #         os.makedirs(path, exist_ok=True)
    #
    #     # CALCULATE POWER based on VARIANT flux
    #     if calc_power:
    #         self.log('info', 'Calculating core power profile')
    #         self.power = calc_power_VARIANT(dassh_input.data, self.path)
    #     else:  # Go find it in the working directory
    #         self.log('info', 'Reading core power profile')
    #         self.power = import_power_VARIANT(dassh_input.data, self.path)
    #
    #     # Core axial fine mesh boundaries - from GEODST and user input
    #     # self.axial_bnds = np.array([np.around(zfi / 100, 12) for zfi in
    #     #                             self.power.z_finemesh])
    #     ax_bnd = [np.around(z / 100, 12) for z in self.power.z_finemesh]
    #     if dassh_input.data['Setup']['Options']['axial_plane'] is not None:
    #         for z in dassh_input.data['Setup']['Options']['axial_plane']:
    #             if not np.around(z, 12) in ax_bnd:  # only add if unique
    #                 ax_bnd.append(np.around(z, 12))
    #     self.axial_bnds = np.sort(ax_bnd)
    #     self.core_length = self.axial_bnds[-1]
    #     self.total_power = np.sum(self.power.power)
    #     msg = 'Total power (W): {:.1f}'.format(self.total_power)
    #     self.log('info', msg)
    #
    #     # Set up DASSH Material objects for each material specified
    #     # self.log('info', 'Loading material properties')
    #     self.materials = dassh_input.materials
    #
    #     # Set up DASSH Assemblies by first creating templates, then
    #     # cloning them into each specified position in the core
    #     self.log('info', 'Generating Assembly objects')
    #     self._assembly_template_setup(dassh_input)
    #     self._assembly_setup(dassh_input)
    #     # if not any([a.rodded for a in self.assemblies]):
    #     #     self.log('error', ('At least one rodded assembly is '
    #     #                        'required to execute DASSH.'))
    #
    #     # Set up DASSH Core object; first need to calculate inter-
    #     # assembly gap flow rate based on total flow rate to the
    #     # assemblies.
    #     self.log('info', 'Generating Core object')
    #     self.flow_rate = self._calculate_total_fr(dassh_input)
    #     self._core_setup(dassh_input)
    #     self._is_adiabatic = False
    #     if dassh_input.data['Core']['gap_model'] is None:
    #         self._is_adiabatic = True
    #     msg = 'Total flow rate (kg/s): {:.4f}'.format(self.flow_rate)
    #     self.log('info', msg)
    #
    #     # Raise warning if est. coolant temp will exceed extreme limit
    #     self._melt_warning(dassh_input, T_max=1500)
    #
    #     # Take the minimum dz required; round down a little bit (this
    #     # just adds some buffer relative to the numerical constraint)
    #     self.req_dz = np.floor(np.min(self.min_dz['dz']) * 1e6) / 1e6
    #     self.log('info', f'Axial step size required (m): {self.req_dz}')
    #     if (self._options['axial_mesh_size'] is not None
    #             and self._options['axial_mesh_size'] <= self.req_dz):
    #         self.req_dz = self._options['axial_mesh_size']
    #         self.log('info', 'Using user-requested axial step '
    #                          'size (m): {:f}'.format(
    #                              self._options["axial_mesh_size"]))
    #     else:
    #         if (self._options['axial_mesh_size'] is not None
    #                 and self._options['axial_mesh_size'] > self.req_dz):
    #             self.log('info', 'Ignoring user-requested axial step '
    #                              'size {:f} m; too large to maintain '
    #                              'numerical stability'.format(
    #                                  self._options["axial_mesh_size"]))
    #         if self.req_dz > 0.01:
    #             self.req_dz = 0.01
    #             self.log('info', 'Reducing step size to improve '
    #                              'accuracy; new step size (m): '
    #                              f'{self.req_dz}')
    #     self.z, self.dz = self._setup_zpts()
    #     self.log('info', f'{len(self.z) - 1} axial steps required')
    #     # Warn if axial steps too small (< 0.5 mm) or too many (> 4k)
    #     if self.req_dz < 0.0005 or len(self.z) - 1 > 2500:
    #         msg = ('Your axial step size is really small so this '
    #                'problem might take a while to solve; consider '
    #                'checking your input for flow maldistribution.')
    #         self.log('warning', msg)
    #
    #     # Generate general output file
    #     if self._options['write_output']:
    #         self.write_summary()

    def _setup_options(self, inp, **kwargs):
        """Store user options from input/invocation"""
        opt = inp.data['Setup']['Options']

        # Load defaults where they aren't be taken from input
        self._options = {}
        self._options['conv_approx_dz_cutoff'] = 0.001
        self._options['write_output'] = False
        self._options['log_progress'] = False
        self._options['parallel'] = False

        # Process user input
        self._options['dif3d_idx'] = opt['dif3d_indexing']
        self._options['axial_plane'] = opt['axial_plane']

        if 'write_output' in kwargs.keys():  # always True in __main__
            self._options['write_output'] = kwargs['write_output']

        self._options['debug'] = opt['debug']
        if 'debug' in kwargs.keys():
            self._options['debug'] = kwargs['debug']

        self._options['axial_mesh_size'] = opt['axial_mesh_size']
        if 'axial_mesh_size' in kwargs.keys():
            self._options['axial_mesh_size'] = kwargs['axial_mesh_size']

        if opt['log_progress'] > 0:
            self._options['log_progress'] = True
            self._options['log_interval'] = opt['log_progress']
            self._stepcount = 0.0

        # Low-flow convection approximation
        self._options['conv_approx'] = opt['conv_approx']
        if opt['conv_approx_dz_cutoff'] is not None:
            self._options['conv_approx_dz_cutoff'] = \
                opt['conv_approx_dz_cutoff']

        self._options['ebal'] = opt['calc_energy_balance']
        if 'calc_energy_balance' in kwargs.keys():
            self._options['ebal'] = kwargs['calc_energy_balance']

        # DUMP FILE ARGUMENTS: collect to set up files at sweep time
        self._options['dump'] = inp.data['Setup']['Dump']
        # Overwrite with kw arguments
        for k in self._options['dump']:
            if k in kwargs.keys():
                self._options['dump'][k] = kwargs[k]
        if self._options['dump']['all']:
            for k in self._options['dump'].keys():
                self._options['dump'][k] = True
        self._options['dump']['any'] = False
        if any(self._options['dump'].values()):
            self._options['dump']['any'] = True

    def _setup_power(self, inp, calc_power_flag):
        """Create the power distributions from ARC binary files or
        user specifications

        Parameters
        ----------
        inp : DASSH_Input object
        calc_power_flag : bool
            Flag indicating whether to run VARPOW to calculate power
            distribution from ARC binary files

        """
        self.power = {}

        # 1. Calculate power based on VARIANT flux
        if True:  # This needs to be a check whether binary files exist
            if calc_power_flag:
                msg = ('Calculating core power profile from CCCC '
                       'binary files')
                self.log('info', msg)
                self.power['dif3d'] = \
                    calc_power_VARIANT(inp.data, self.path)
            else:  # Go find it in the working directory
                msg = ('Reading core power profile from VARPOW '
                       'output files')
                self.log('info', msg)
                self.power['dif3d'] = \
                    import_power_VARIANT(inp.data, self.path)

        # 2. Read user power, if given
        if inp.data['Power']['user_power'] is not None:
            msg = ('Reading user-specified power profiles from '
                   + inp.data['Power']['user_power'])
            self.power['user'] = \
                dassh.power._from_file(inp.data['Power']['user_power'])

    def _setup_axial_region_bnds(self, inp):
        """Get axial mesh points from ARC binary files, user-specified
        power distribution, and user input file request

        Parameters
        ----------
        inp : DASSH_Input object

        Returns
        -------
        None

        """
        # Accumulate all values in list
        ax_bnd = []

        # DIF3D binary files
        if 'dif3d' in self.power.keys():
            ax_bnd += list(self.power['dif3d'].z_finemesh * 1e-2)

        # User power specification
        if 'user' in self.power.keys():
            for ai in range(len(self.power['user'])):
                ax_bnd += list(self.power['user'][ai][1]['zfm'])

        # Axial regions in assembly specification
        for a in inp.data['Assembly'].keys():
            tmp = inp.data['Assembly'][a]['AxialRegion']
            for r in tmp.keys():
                ax_bnd.append(tmp[r]['z_lo'])
                ax_bnd.append(tmp[r]['z_hi'])

        # User axial boundary request
        if self._options['axial_plane'] is not None:
            ax_bnd += self._options['axial_plane']

        # Round values then discard duplicates
        ax_bnd = np.unique(np.around(ax_bnd, 12))
        self.axial_bnds = ax_bnd
        self.core_length = self.axial_bnds[-1]

    def _setup_asm_templates(self, inp):
        """Generate template DASSH Assembly objects based on user input

        Parameters
        ----------
        inp : DASSH_Input object
            Contains "data" attribute with user inputs

        Returns
        -------
        dict
            Dictionary of DASSH assembly objects with placeholder
            positions and coolant mass flow rates

        """
        asm_templates = {}
        mfrx = -1.0  # placeholder for mass flow rate in cloned asm
        # inlet_temp = inp_obj.data['Core']['coolant_inlet_temp']
        cool_mat = inp.data['Core']['coolant_material'].lower()
        for a in inp.data['Assembly'].keys():
            asm_data = inp.data['Assembly'][a]

            # Create materials dictionary
            mat_data = {}
            mat_data['coolant'] = self.materials[cool_mat].clone()
            mat_data['duct'] = self.materials[
                asm_data['duct_material'].lower()].clone()
            if 'FuelModel' in asm_data:
                mat_data['clad'] = self.materials[
                    asm_data['FuelModel']['clad_material'].lower()
                ].clone()
                if asm_data['FuelModel']['gap_material'] is not None:
                    mat_data['gap'] = self.materials[
                        asm_data['FuelModel']['gap_material'].lower()
                    ].clone()
                else:
                    mat_data['gap'] = None
            # make the list of "template" Assembly objects
            asm_templates[a] = dassh.assembly.Assembly(a,
                                                       (-1, -1),
                                                       asm_data,
                                                       mat_data,
                                                       self.inlet_temp,
                                                       mfrx)

        # Store as attribute b/c used later to write summary output
        self.asm_templates = asm_templates

    def _setup_asm_power(self, inp):
        """Generate assembly power profiles

        Parameters
        ----------
        inp : DASSH_Input object

        Returns
        -------
        list
            List of tuples containing assembly power parameters for
            each assembly, arranged by DASSH index
            1. Power profiles for pins, duct, coolant
            2. Average power profile
            3. Total power
            4. Z-mesh that defines power profile axial boundaries

        """
        # Return list of power profiles arranged by DASSH index
        asm_power = []
        core_total_power = 0.0

        # Identify assemblies that have user power specifications;
        # convert to Python index; returns empty list of no user power
        user_power_idx = []
        if 'user' in self.power.keys():
            user_power_idx = [x[0] - 1 for x in self.power['user']]

        for i in range(len(inp.data['Assignment']['ByPosition'])):
            # Pull up assignment and assembly input data
            # k[0]: assembly type : str e.g. its name ("reflector")
            # k[1]: assembly loc : tuple (ring, pos, id)  all base-0
            # k[2]: dict with kwargs
            k = inp.data['Assignment']['ByPosition'][i]
            atype = k[0]

            # The user's choice of "dif3d_indexing" option defines how
            # they've ordered the assemblies in the "Assignment" input
            # section. We need to identify DASSH location to assign it
            # at Assembly object creation, but we also need the DIF3D
            # ID to correctly assign power from DIF3D binary files
            dif3d_id, dassh_id, dassh_loc = \
                identify_asm(k[1][:2], i, self._options['dif3d_idx'])

            # Calculate total power and determine component power
            # profiles, but do not assign to new assembly object.
            # Try to find in user-supplied power
            idx = dif3d_id if self._options['dif3d_idx'] else dassh_id
            if idx in user_power_idx:
                # isolate appropriate user power dictionary
                tmp = self.power['user'][user_power_idx.index(idx)][1]
                power_profile = tmp['power_profiles']
                avg_power_profile = tmp['avg_power']
                z_mesh = tmp['zfm']
                tot_power = np.sum((z_mesh[1:] - z_mesh[:-1])
                                   * avg_power_profile)
                # k_bnds = match_rodded_finemesh_bnds(
                #     z_mesh, inp.data['Assembly'][atype])

                # Need to check that user power input matches assembly
                # assignment geometry (number of pins, etc)
            else:  # Get it from DIF3D power
                power_profile, avg_power_profile = \
                    self.power['dif3d'].calc_power_profile(
                        self.asm_templates[atype], i)
                tot_power = np.sum(self.power['dif3d'].power[dif3d_id])
                z_mesh = self.power['dif3d'].z_finemesh
                # k_bnds = match_rodded_finemesh_bnds_dif3d(
                #     self.power, inp.data['Assembly'][atype])
            # Track total power
            core_total_power += tot_power

            # add to list
            asm_power.append(
                [power_profile,
                 avg_power_profile,
                 tot_power,
                 z_mesh])  # , k_bnds)

        # Scale power as requested by user and assign "total_power"
        # attribute to Reactor object; return assembly power list
        asm_power, total_power = self._setup_scale_asm_power(
            asm_power,
            core_total_power,
            inp.data['Core']['total_power'],
            inp.data['Core']['power_scaling_factor'])
        self.total_power = total_power
        return asm_power

    @staticmethod
    def _setup_scale_asm_power(plist, pcalc, ptot_user, pscalar):
        """Scale assembly power according to user request

        Parameters
        ----------
        plist : list
            List of power profile information for each assembly
        pcalc : float
            Calculated total power
        ptot_user : float
            User-requested core total power
        pscalar : float
            Scaling factor to apply to core total power

        Returns
        -------
        list
            "plist" with items modified to reflect scaled power

        Notes
        -----
        If the user requests a total power normalization and applies
        a scaling factor to the power, the resulting core power will
        be equal to the product of the requested core power and the
        scaling factor.

        """
        # Normalize power to user request
        renorm = 1.0
        if ptot_user != 0.0:
            renorm = ptot_user / pcalc
            for i in range(len(plist)):
                # Component power profiles
                for k in plist[i][0].keys():
                    plist[i][0][k] *= renorm
                # Average power profile
                plist[i][1] *= renorm
                # Total power
                plist[i][2] *= renorm

        # Scale power again if user requested
        if pscalar != 1.0:
            for i in range(len(plist)):
                # Component power profiles
                for k in plist[i][0].keys():
                    plist[i][0][k] *= pscalar
                # Average power profile
                plist[i][1] *= pscalar
                # Total power
                plist[i][2] *= pscalar

        return plist, pcalc * renorm * pscalar

    def _setup_asm_bc(self, inp, power_params):
        """Estimate flow rate or outlet temperature

        Parameters
        ----------
        inp : DASSH_Input object
        power_params : list
            List of tuples generated by _setup_asm_power method

        """
        T_out = []
        flow_rate = []
        for i in range(len(inp.data['Assignment']['ByPosition'])):
            # Pull up assignment and assembly input data
            # k[0]: assembly type : str e.g. its name ("reflector")
            # k[1]: assembly loc : tuple (ring, pos, id)  all base-0
            # k[2]: dict with kwargs
            k = inp.data['Assignment']['ByPosition'][i]
            atype = k[0]

            # The user's choice of "dif3d_indexing" option defines how
            # they've ordered the assemblies in the "Assignment" input
            # section. We need to identify DASSH location to assign it
            # at Assembly object creation, but we also need the DIF3D
            # ID to correctly assign power from DIF3D binary files
            dif3d_id, dassh_id, dassh_loc = \
                identify_asm(k[1][:2], i, self._options['dif3d_idx'])

            # Pull assembly power from power parameters list
            asm_power = power_params[i][2]
            if 'flowrate' in k[2].keys():  # estimate outlet temp
                flow_rate_tmp = k[2]['flowrate']
                T_out_tmp = dassh.utils.Q_equals_mCdT(
                    asm_power,
                    self.inlet_temp,
                    self.asm_templates[atype].active_region.coolant,
                    mfr=flow_rate_tmp)
            elif 'outlet_temp' in k[2].keys():  # estimate flow rate
                T_out_tmp = k[2]['outlet_temp']
                flow_rate_tmp = dassh.utils.Q_equals_mCdT(
                    asm_power,
                    self.inlet_temp,
                    self.asm_templates[atype].active_region.coolant,
                    t_out=T_out_tmp)
            else:
                msg = ('Could not estimate flow rate / outlet temp'
                       f'for asm no. {id} ({atype}) from given inputs')
                self.log('error', msg)

            T_out.append(T_out_tmp)
            flow_rate.append(flow_rate_tmp)
        return T_out, flow_rate

    def _setup_asm(self, inp, asm_power, To, fr):
        """Generate a list of DASSH assemblies and determine the minimum
        axial mesh size required for numerical stability.

        Parameters
        ----------
        inp_obj : DASSH Input object
            User inputs to DASSH

        Returns
        -------
        list
            Assemblies in the core, ordered by position index
        float
            Minimum axial mesh size required for core-wide stability

        Notes
        -----
        1. Identify assembly index and location based on user input.
        2. Calculate total power and determine component power
           profiles but do not assign to new assembly object.
        3. Using total power, estimate outlet temperature and flow
           rate as necessary.
        4. Clone assembly object from template using flow rate and
           assign power profiles.
        5. Store outlet temperature estimate to use when determining
           axial mesh size requirement.

        """
        # List of assemblies to populate
        assemblies = []
        for i in range(len(inp.data['Assignment']['ByPosition'])):
            # Pull up assignment and assembly input data
            # k[0]: assembly type : str e.g. its name ("reflector")
            # k[1]: assembly loc : tuple (ring, pos, id)  all base-0
            # k[2]: dict with kwargs
            k = inp.data['Assignment']['ByPosition'][i]
            atype = k[0]
            asm_data = inp.data['Assembly'][atype]

            # The user's choice of "dif3d_indexing" option defines how
            # they've ordered the assemblies in the "Assignment" input
            # section. We need to identify DASSH location to assign it
            # at Assembly object creation, but we also need the DIF3D
            # ID to correctly assign power from DIF3D binary files
            dif3d_id, dassh_id, dassh_loc = \
                identify_asm(k[1][:2], i, self._options['dif3d_idx'])

            # Power scaling for individual assemblies: WARNING
            # This is only meant to be a developer feature to test
            # heat transfer between assemblies. It will ruin the
            # normalization of power to the fixed value requested
            # in the input
            power_scalar = 1.0
            # if self._options['debug']:
            #     if 'scale_power' in k[2].keys():
            #         power_scalar = k[2]['scale_power']
            # asm_power *= power_scalar

            # Clone assembly object from template using flow rate
            # and assign power profiles
            asm = self.asm_templates[atype].clone(
                dassh_loc, new_flowrate=fr[i])

            bundle_bnd = get_rod_bundle_bnds(asm_power[i][3], asm_data)
            asm.power = dassh.power.AssemblyPower(asm_power[i][0],
                                                  asm_power[i][1],
                                                  asm_power[i][3],
                                                  bundle_bnd,
                                                  scale=power_scalar)
            asm.total_power = asm_power[i][2]
            asm._estimated_T_out = To[i]
            assemblies.append(asm)

        # Sort the assemblies according to the DASSH assembly ID
        assemblies.sort(key=lambda x: x.id)
        self.assemblies = assemblies

    def _setup_asm_axial_mesh_req(self):
        """Calculate the required axial mesh size for each assembly"""
        self.min_dz = {}
        self.min_dz['dz'] = []  # The step size required by each asm
        self.min_dz['sc'] = []  # Code for limiting subchannel type
        for ai in range(len(self.assemblies)):
            asm = self.assemblies[ai]
            # Calculate minumum dz (based on geometry and flow rate);
            # if min dz is constrained by edge/corner subchannel, use
            # SE2ANL model rather than DASSH model to relax constraint
            dz, sc = dassh.assembly.calculate_min_dz(
                asm, self.inlet_temp, asm._estimated_T_out)

            use_conv_approx = False
            if self._options['conv_approx']:
                if dz < self._options['conv_approx_dz_cutoff']:
                    if asm.has_rodded:
                        if sc[0] in ['2', '3', '6', '7']:
                            use_conv_approx = True
                    else:
                        use_conv_approx = True
            if use_conv_approx:
                if self._options['dif3d_idx']:
                    id = asm.dif3d_id
                else:
                    id = asm.id
                dz_old = dz
                msg1 = ('Assembly {:d} mesh size requirement {:s} is '
                        'too small (dz = {:.2e} m);')
                msg2 = ('    Treating duct wall connection with '
                        ' modified approach that yields dz = {:.2e} m.')
                for reg in self.assemblies[ai].region:
                    reg._lowflow = True
                dz, sc = dassh.assembly.calculate_min_dz(
                    asm, self.inlet_temp, asm._estimated_T_out)
                self.log('info', msg1.format(id, str(sc), dz_old))
                self.log('info', msg2.format(dz))
            self.min_dz['dz'].append(dz)
            self.min_dz['sc'].append(sc)

    def _assembly_template_setup(self, inp):
        """Generate template DASSH Assembly objects based on user input

        Parameters
        ----------
        inp : DASSH_Input object
            Contains "data" attribute with user inputs

        Returns
        -------
        dict
            Dictionary of DASSH assembly objects with placeholder
            positions and coolant mass flow rates

        """
        asm_templates = {}
        mfrx = -1.0  # placeholder for mass flow rate in cloned asm
        # inlet_temp = inp_obj.data['Core']['coolant_inlet_temp']
        cool_mat = inp.data['Core']['coolant_material'].lower()
        for a in inp.data['Assembly'].keys():
            asm_data = inp.data['Assembly'][a]

            # Create materials dictionary
            mat_data = {}
            mat_data['coolant'] = self.materials[cool_mat].clone()
            mat_data['duct'] = self.materials[
                asm_data['duct_material'].lower()].clone()
            if 'FuelModel' in asm_data:
                mat_data['clad'] = self.materials[
                    asm_data['FuelModel']['clad_material'].lower()
                ].clone()
                if asm_data['FuelModel']['gap_material'] is not None:
                    mat_data['gap'] = self.materials[
                        asm_data['FuelModel']['gap_material'].lower()
                    ].clone()
                else:
                    mat_data['gap'] = None
            # make the list of "template" Assembly objects
            asm_templates[a] = dassh.assembly.Assembly(a,
                                                       (-1, -1),
                                                       asm_data,
                                                       mat_data,
                                                       self.inlet_temp,
                                                       mfrx)
        self.asm_templates = asm_templates

    def _assembly_setup(self, inp_obj):
        """Generate a list of DASSH assemblies and determine the minimum
        axial mesh size required for numerical stability.

        Parameters
        ----------
        inp_obj : DASSH Input object
            User inputs to DASSH

        Returns
        -------
        list
            Assemblies in the core, ordered by position index
        float
            Minimum axial mesh size required for core-wide stability

        """
        assemblies = []
        self.min_dz = {}
        self.min_dz['dz'] = []  # The step size required by each asm
        self.min_dz['sc'] = []  # Code for limiting subchannel type
        for i in range(len(inp_obj.data['Assignment']['ByPosition'])):
            # Pull up assignment and assembly input data
            # k[0]: assembly type : str e.g. its name ("reflector")
            # k[1]: assembly position : tuple
            #       (ring, position, id)  all in base-0 index
            # k[2]: dict with kwargs
            k = inp_obj.data['Assignment']['ByPosition'][i]
            # print(k)
            atype = k[0]
            aloc = k[1][:2]
            if inp_obj.data['Setup']['Options']['dif3d_indexing']:
                aloc = dassh.utils.dif3d_loc_to_dassh_loc(k[1][:2])
                id = i
            else:
                id = dassh.utils.dassh_loc_to_dif3d_id(k[1][:2])

            # Estimate flow rate OR outlet temp - need flow rate for
            # assembly instantiation; need temperature to calculate dz
            asm_power = np.sum(self.power.power[id])

            # Power scaling for individual assemblies: WARNING
            # This is only meant to be a developer feature to test
            # heat transfer between assemblies. It will ruin the
            # normalization of power to the fixed value requested
            # in the input
            power_scalar = 1.0
            if self._options['debug']:
                if 'scale_power' in k[2].keys():
                    power_scalar = k[2]['scale_power']
            asm_power *= power_scalar

            if 'flowrate' in k[2].keys():  # estimate outlet temp
                flow_rate = k[2]['flowrate']
                T_out = dassh.utils.Q_equals_mCdT(
                    asm_power,
                    self.inlet_temp,
                    self.asm_templates[atype].active_region.coolant,
                    mfr=flow_rate)
            elif 'outlet_temp' in k[2].keys():  # estimate flow rate
                T_out = k[2]['outlet_temp']
                flow_rate = dassh.utils.Q_equals_mCdT(
                    asm_power,
                    self.inlet_temp,
                    self.asm_templates[atype].active_region.coolant,
                    t_out=T_out)
            else:
                msg = ('Could not estimate flow rate / outlet temp'
                       f'for asm no. {id} ({atype}) from given inputs')
                self.log('error', msg)

            # Create assembly object
            asm = self.asm_templates[atype].clone(
                aloc, new_flowrate=flow_rate)

            # Calculate minumum dz (based on geometry and flow rate);
            # if min dz is constrained by edge/corner subchannel, use
            # SE2ANL model rather than DASSH model to relax constraint
            dz, sc = dassh.assembly.calculate_min_dz(
                asm, self.inlet_temp, T_out)
            # dz_limit = 0.001
            use_conv_approx = False
            if self._options['conv_approx']:
                if dz < self._options['conv_approx_dz_cutoff']:
                    if asm.has_rodded:
                        if sc[0] in ['2', '3', '6', '7']:
                            use_conv_approx = True
                    else:
                        use_conv_approx = True
            if use_conv_approx:
                if self._options['dif3d_idx']:
                    id = asm.dif3d_id
                else:
                    id = asm.id
                dz_old = dz
                msg1 = ('Assembly {:d} mesh size requirement {:s} is '
                        'too small (dz = {:.2e} m);')
                msg2 = ('    Treating duct wall connection with '
                        ' modified approach that yields dz = {:.2e} m.')
                for reg in asm.region:
                    reg._lowflow = True
                dz, sc = dassh.assembly.calculate_min_dz(
                    asm, self.inlet_temp, T_out)
                self.log('info', msg1.format(id, str(sc), dz_old))
                self.log('info', msg2.format(dz))

            # Add power profiles to new assembly
            # power_prof = self.power[atype].calc_power_profile(asm, id)
            power_prof, avg_power_prof = \
                self.power.calc_power_profile(asm, id)

            # Determine bounds of core rodded region
            k_bnds = match_rodded_finemesh_bnds(
                self.power, inp_obj.data['Assembly'][atype])

            asm.power = dassh.power.AssemblyPower(
                power_prof,
                avg_power_prof,
                self.power.z_finemesh,
                k_bnds,
                scale=power_scalar)

            # Store the total assembly power
            asm.total_power = asm_power

            # Add to the assemblies, min_dz lists
            assemblies.append(asm)
            self.min_dz['dz'].append(dz)
            self.min_dz['sc'].append(sc)

        # Sort the assemblies according to the DASSH assembly ID
        assemblies.sort(key=lambda x: x.id)
        self.assemblies = assemblies

    def _calculate_total_fr(self, inp_obj):
        """Calculate core-total flow rate"""
        tot_fr = 0.0
        for a in self.assemblies:
            tot_fr += a.flow_rate
        tot_fr = tot_fr / (1 - inp_obj.data['Core']['bypass_fraction'])
        return tot_fr

    def _core_setup(self, inp_obj):
        """Set up DASSH Core object using GEODST and the parameters from
        each assembly in in the core"""
        geodst = py4c.geodst.GEODST(
            os.path.join(inp_obj.path,
                         inp_obj.data['Neutronics']['geodst'][0]))

        # Interassembly gap flow rate
        gap_fr = inp_obj.data['Core']['bypass_fraction'] * self.flow_rate

        # Estimate outlet temperature based on core power
        # print(t_in, self.total_power, total_fr)
        cool_mat = inp_obj.data['Core']['coolant_material'].lower()
        # print(self.total_power, self.inlet_temp, self.flow_rate)
        t_out = dassh.utils.Q_equals_mCdT(self.total_power,
                                          self.inlet_temp,
                                          self.materials[cool_mat],
                                          mfr=self.flow_rate)

        # Instantiate and load core object
        core_obj = dassh.core.Core(
            geodst,
            gap_fr,
            self.materials[inp_obj.data['Core']['coolant_material'].lower()],
            inlet_temperature=self.inlet_temp,
            model=inp_obj.data['Core']['gap_model'])
        core_obj.load(self.assemblies)
        self.core = core_obj

        # Calculate dz required for numerical stability
        dz, sc = dassh.core.calculate_min_dz(
            core_obj, self.inlet_temp, t_out)
        if dz is not None:
            self.min_dz['dz'].append(dz)
            self.min_dz['sc'].append(sc)

        # Track whether the Reactor is adiabatic
        self._is_adiabatic = False
        if inp_obj.data['Core']['gap_model'] is None:
            self._is_adiabatic = True

    def _setup_overall_axial_mesh_req(self):
        """Evaluate axial mesh size for core and adjust based on user
        request or to ensure numerical accuracy"""
        # Take the minimum dz required; round down a little bit (this
        # just adds some buffer relative to the numerical constraint)
        self.req_dz = np.floor(np.min(self.min_dz['dz']) * 1e6) / 1e6
        self.log('info', f'Axial step size required (m): {self.req_dz}')
        if (self._options['axial_mesh_size'] is not None
                and self._options['axial_mesh_size'] <= self.req_dz):
            self.req_dz = self._options['axial_mesh_size']
            self.log('info', 'Using user-requested axial step '
                             'size (m): {:f}'.format(
                                 self._options["axial_mesh_size"]))
        else:
            if (self._options['axial_mesh_size'] is not None
                    and self._options['axial_mesh_size'] > self.req_dz):
                self.log('info', 'Ignoring user-requested axial step '
                                 'size {:f} m; too large to maintain '
                                 'numerical stability'.format(
                                     self._options["axial_mesh_size"]))
            if self.req_dz > 0.01:
                self.req_dz = 0.01
                self.log('info', 'Reducing step size to improve '
                                 'accuracy; new step size (m): '
                                 f'{self.req_dz}')

    def _melt_warning(self, inp_obj, T_max):
        """Raise error if the user has not provided enough flow to
        the reactor such that extreme temperatures are likely"""
        _MELT_MSG = ('Estimated coolant outlet temperature {0} is '
                     'greater than limit {1}')
        cool_mat = inp_obj.data['Core']['coolant_material'].lower()
        cool_obj = self.materials[cool_mat]
        T_out = dassh.utils.Q_equals_mCdT(self.total_power,
                                          self.inlet_temp,
                                          cool_obj, mfr=self.flow_rate)
        if T_out > T_max:
            self.log('warning', _MELT_MSG.format(T_out, T_max))

    def _setup_zpts(self):
        """Based on calculated dz mesh constraint and axial region
        bounds, determine points to calculate solutions"""
        z = [0.0]
        dz = []
        while z[-1] < self.core_length:
            dz.append(self._check_dz(z[-1]))
            z.append(np.around(z[-1] + dz[-1], 12))
        return np.array(z), np.array(dz)

    def _check_dz(self, z):
        """Make sure that axial step z + dz does not cross any region
        boundaries; if it does, modify dz to meet the boundary plane

        Parameters
        ----------
        z : float
            Axial mesh point

        Returns
        -------
        float
            Axial mesh size step that doesn't cross region boundary

        Notes
        -----
        This should also keep the solution from progressing beyond the
        length of the core, as that should be the last value in the
        region_bounds array.

        """
        z = np.around(z, 12)
        cross_boundary = [z < bi and z + self.req_dz > bi
                          for bi in self.axial_bnds]
        if not any(cross_boundary):
            return self.req_dz
        else:
            crossed_bound = np.where(cross_boundary)[0][0]
            return np.around(self.axial_bnds[crossed_bound] - z, 12)

    def save(self, path=None):
        """Save the Reactor object as a file for later use"""
        if path is None:
            path = self.path

        # Close the open data files
        try:
            self._data_close()
        except (KeyError, AttributeError):  # no open data
            pass

        with open(os.path.join(path, 'dassh_reactor.pkl'), 'wb') as f:
            # cPickle.dump(self, f, cPickle.HIGHEST_PROTOCOL)
            pickle.dump(self, f, protocol=pickle.DEFAULT_PROTOCOL)

    def reset(self):
        """Reset all the temperatures back to the inlet temperature"""
        self.core.coolant_gap_temp *= 0.0
        self.core.coolant_gap_temp += self.inlet_temp
        for i in range(len(self.assemblies)):
            for j in range(len(self.assemblies[i].region)):
                for k in self.assemblies[i].region[j].temp.keys():
                    self.assemblies[i].region[j].temp[k] *= 0.0
                    self.assemblies[i].region[j].temp[k] += \
                        self.inlet_temp

    def _data_setup(self):
        """Set up the data files for the temperature dumps"""
        # If no data dump requested, skip this step
        if not self._options['dump']['any']:
            return

        if self._options['dump']['interval'] is not None:
            self.log('info', 'Dumping temperatures with interval of '
                             '{:f} m and at all requested axial '
                             'positions'.format(
                                 self._options['dump']['interval']))
        else:
            self.log('info', 'Dumping temperatures at every axial step')

        self._options['dump']['dz'] = 0.0

        # Data that we're tracking
        self._options['dump']['names'] = []
        _msg = 'Dumping {:s} temperatures to \"{:s}\"'
        if self._options['dump']['coolant']:
            self._options['dump']['names'].append('coolant_int')
            self.log('info', _msg.format('interior coolant',
                                         'temp_coolant_int.csv'))
            if any([a.rodded.n_bypass > 0 for a in
                    self.assemblies if a.has_rodded]):
                self._options['dump']['names'].append('coolant_byp')
                self.log('info', _msg.format('bypass coolant',
                                             'temp_coolant_byp.csv'))
        if self._options['dump']['duct']:
            self._options['dump']['names'].append('duct_mw')
            self.log('info', _msg.format('duct mid-wall',
                                         'temp_duct_mw.csv'))
        if self._options['dump']['gap']:
            self._options['dump']['names'].append('coolant_gap')
            self.log('info', _msg.format('interassembly gap coolant',
                                         'temp_coolant_gap.csv'))
        if self._options['dump']['pins']:
            self._options['dump']['names'].append('pin')
            self.log('info', _msg.format('pin', 'temp_pin.csv'))
        if self._options['dump']['average']:
            self._options['dump']['names'].append('average')
            self.log('info', _msg.format('average coolant and pin',
                                         'temp_average.csv'))
        if self._options['dump']['maximum']:
            self._options['dump']['names'].append('maximum')
            self.log('info', _msg.format('maximum coolant and pin',
                                         'temp_maximum.csv'))

        # Set up dictionary of paths to data
        self._options['dump']['paths'] = {}
        for f in self._options['dump']['names']:
            name = f'temp_{f}'
            fullname = f'{name}.csv'
            if os.path.exists(os.path.join(self.path, fullname)):
                os.remove(os.path.join(self.path, fullname))
            self._options['dump']['paths'][f] = \
                os.path.join(self.path, fullname)

        # Set up data columns
        self._options['dump']['cols'] = {}
        self._options['dump']['cols']['average'] = 11
        self._options['dump']['cols']['maximum'] = 8
        self._options['dump']['cols']['coolant_int'] = 4 + max(
            [a.rodded.subchannel.n_sc['coolant']['total']
             for a in self.assemblies if a.has_rodded])
        self._options['dump']['cols']['duct_mw'] = 5 + max(
            [a.rodded.subchannel.n_sc['duct']['total']
             for a in self.assemblies if a.has_rodded])
        self._options['dump']['cols']['coolant_byp'] = 5 + max(
            [a.rodded.subchannel.n_sc['bypass']['total']
             for a in self.assemblies if a.has_rodded])
        self._options['dump']['cols']['coolant_gap'] = \
            1 + len(self.core.coolant_gap_temp)
        self._options['dump']['cols']['pin'] = 10

        for a in self.assemblies:
            a.setup_data_io(self._options['dump']['cols'])

    def _data_open(self):
        """Open the data files to which the temperature data will be
        dumped throughout the sweep"""
        if not self._options['dump']['any']:
            return

        self._options['dump']['files'] = {}
        for f in self._options['dump']['names']:
            self._options['dump']['files'][f] = \
                open(self._options['dump']['paths'][f], 'ab')

    def _data_close(self):
        """Close the data files"""
        for k in self._options['dump']['files'].keys():
            self._options['dump']['files'][k].close()
            self._options['dump']['files'][k] = None

    ####################################################################
    # TEMPERATURE SWEEP
    ####################################################################

    def temperature_sweep(self, verbose=False):
        """Sweep axially through the core, solving coolant and duct
        temperatures at each level

        Parameters
        ----------
        verbose (optional) : bool
            Print data from each step during sweep (default False)


        Returns
        -------
        None

        """
        # Open the CSV files to which data is dumped throughout the
        # problem; these are left open and written to at each step
        self._data_setup()
        self._data_open()

        if self._options['parallel']:
            pool = mp.Pool()

        # Track the time elapsed
        self._starttime = time.time()

        for i in range(1, len(self.z)):
            # Calculate temperatures
            if self._options['parallel']:
                self.axial_step_parallel(self.z[i], self.dz[i - 1], pool)
            else:
                self.axial_step(self.z[i], self.dz[i - 1], verbose)

            # Log progress, if requested
            if self._options['log_progress']:
                self._stepcount += 1
                if self._options['log_interval'] <= self._stepcount:
                    self._print_log_msg(i)

        # Once the sweep is done close the CSV data files, if open
        if self._options['parallel']:
            pool.close()
            pool.join()
        try:
            self._data_close()
        except (AttributeError, KeyError):
            pass

        # write the summary output
        if self._options['write_output']:
            self.write_output_summary()

    def _print_log_msg(self, step):
        """Format the message to log to the screen"""
        # Format plane number and axial position
        s_fmt = str(step).rjust(4)
        z_fmt = '{:.2f}'.format(self.z[step])
        # Format time elapsed
        _end = time.time()
        hours, rem = divmod(_end - self._starttime, 3600)
        minutes, seconds = divmod(rem, 60)
        elapsed = "{:0>2}:{:0>2}:{:05.2f}".format(
            int(hours), int(minutes), seconds)
        # Print message
        msg = (f'Progress: plane {s_fmt} of '
               f'{len(self.dz)}; z = {z_fmt} m; '
               f'cumulative sweep time = {elapsed}')
        self.log('info', msg)
        self._stepcount = 0

    def axial_step(self, z, dz, verbose=False):
        """Solve temperatures at the next axial step

        Parameters
        ----------
        z : float
            Absolute axial position (m)
        dz : float
            Axial mesh size (m)
        verbose (optional) : bool
            Indicate whether to print step summary

        """
        # First, some administrative crap: figure out whether you're
        # dumping temperatures at this axial step
        dump_step = self._determine_whether_to_dump_data(z, dz)

        # 1. Calculate gap coolant temperatures at the j+1 level
        #    based on duct wall temperatures at the j level.
        if self.core.model is not None:
            t_duct = np.array(
                [approximate_temps(a.x_pts,
                                   a.duct_outer_surf_temp,
                                   self.core.x_pts,
                                   a._lstsq_params)
                 for a in self.assemblies])
            self.core.calculate_gap_temperatures(dz, t_duct)

        # 2. Calculate assembly coolant and duct temperatures.
        #    Different treatment depending on whether in the
        #    heterogeneous or homogeneous region; varies assembly to
        #    assembly, handled in the same method.
        #    (a) Heterogeneous region:
        #        - Calculate assembly coolant temperatures at the j+1
        #          level based on coolant and duct wall temepratures
        #          at the j level
        #        - Calculate assembly duct wall temperatures at the j+1
        #          level based on assembly and gap coolant temperatures
        #          at the j+1 level
        #    (b) Homogeneous region (porous media)
        #        - Calculate assembly coolant and duct temepratures at
        #          the j+1 level based on temperatures at the j level
        for asm in self.assemblies:
            self._calculate_asm_temperatures(asm, z, dz, dump_step)

        if verbose:
            print(self._print_step_summary(z, dz))

    def axial_step_parallel(self, z, dz, worker_pool):
        """Parallelized version of axial_step

        Parameters
        ----------
        z : float
            Absolute axial position (m)
        dz : float
            Axial mesh size (m)
        verbose (optional) : bool
            Indicate whether to print step summary
        worker_pool : multiprocessing Pool object
            Workers to perform parallel tasks

        """
        # First, some administrative crap: figure out whether you're
        # dumping temperatures at this axial step
        dump_step = self._determine_whether_to_dump_data(z, dz)

        # 1. Calculate gap coolant temperatures at the j+1 level
        #    based on duct wall temperatures at the j level.
        if self.core.model is not None:
            # worker_pool = mp.Pool()
            t_duct = []
            for asm in self.assemblies:
                t_duct.append(
                    worker_pool.apply_async(
                        approximate_temps,
                        args=(asm.x_pts,
                              asm.duct_outer_surf_temp,
                              self.core.x_pts,
                              asm._lstsq_params, )
                    )
                )
            t_duct = np.array([td.get() for td in t_duct])
            self.core.calculate_gap_temperatures(dz, t_duct)
            # worker_pool.close()
            # worker_pool.join()
        # 2. Calculate assembly coolant and duct temperatures.
        #    Different treatment depending on whether in the
        #    heterogeneous or homogeneous region; varies assembly to
        #    assembly, handled in the same method.
        updated_asm = []
        # worker_pool = mp.Pool()
        for asm in self.assemblies:
            updated_asm.append(
                worker_pool.apply_async(
                    self._calculate_asm_temperatures,
                    args=(asm, z, dz, dump_step, ),
                    error_callback=err_cb))
        self.assemblies = [a.get() for a in updated_asm]
        # worker_pool.close()
        # worker_pool.join()

        # Write the results
        if dump_step:
            for asm in self.assemblies:
                asm.write(self._options['dump']['files'], None)

    def _determine_whether_to_dump_data(self, z, dz):
        """Dump data to CSV if interval length is reached or if at
        an axial region boundary"""
        if self._options['dump']['any']:
            self._options['dump']['dz'] += dz
            # No interval given; dump at every step
            if self._options['dump']['interval'] is None:
                dump_step = True
            # Interval provided, and we need to write data and reset
            elif (np.around(self._options['dump']['dz'], 9)
                    >= self._options['dump']['interval']):
                dump_step = True
                self._options['dump']['dz'] = 0.0
            # Axial plane requested by user; write data, no reset
            elif z in self.axial_bnds:
                dump_step = True
            # Don't do anything
            else:
                dump_step = False
        else:
            dump_step = False
        return dump_step

    def _calculate_asm_temperatures(self, asm, z, dz, dump_step):
        """Calculate assembly coolant and duct temperatures"""
        # Update the region if necessary
        asm.check_region_update(z)
        # Find and approximate gap temperatures next to each asm
        gap_adj_temps = approximate_temps(
            self.core.x_pts,
            self.core.adjacent_coolant_gap_temp(asm.id),
            asm.x_pts,
            self.core._lstsq_params)
        asm.calculate(z, dz, gap_adj_temps,
                      self.core.coolant_gap_params['htc'],
                      self._is_adiabatic, self._options['ebal'])
        # Write the results
        if dump_step:
            asm.write(self._options['dump']['files'], gap_adj_temps)
        return asm

    def _print_step_summary(self, z, dz):
        """Print some stuff about assembly power and coolant
        and duct temperatures at the present axial level"""
        to_print = []
        to_print.append(z)
        to_print.append(dz)
        for asm in self.assemblies:
            p = asm.power.get_power(z)
            total_power = 0.0
            for k in p.keys():
                if p[k] is not None:
                    total_power += np.sum(p[k])
            to_print.append(total_power * dz)
            to_print.append(asm.avg_coolant_temp)
            to_print += list(asm.avg_duct_mw_temp)
        to_print.append(self.core.avg_coolant_gap_temp)
        return ' '.join(['{:.10e}'.format(v) for v in to_print])

    ####################################################################
    # WRITE OUTPUT
    ####################################################################

    def write_summary(self):
        """Write the main DASSH output file"""
        # Output file preamble
        out = 'DASSH: Ducted Assembly Steady-State Heat Transfer Code\n'
        out += f'Version {dassh.__version__}\n'
        out += f'Executed {str(datetime.datetime.now())}\n'

        # Geometry summary
        geom = dassh.table.GeometrySummaryTable(len(self.asm_templates))
        out += geom.generate(self)

        # Power summary
        power = dassh.table.PositionAssignmentTable()
        out += power.generate(self)

        # Flow summary
        flow = dassh.table.CoolantFlowTable()
        out += flow.generate(self)

        # Write to output file
        with open(os.path.join(self.path, 'dassh.out'), 'w') as f:
            f.write(out)

    def write_output_summary(self):
        """Write the main DASSH output file"""
        out = ''

        # Pressure drop
        dp = dassh.table.PressureDropTable(
            max([len(a.region) for a in self.assemblies]))
        out += dp.generate(self)

        # Assembly energy balance
        if self._options['ebal']:
            asm_ebal_table = dassh.table.AssemblyEnergyBalanceTable()
            out += asm_ebal_table.generate(self)

        # Core energy balance
        if self._options['ebal']:
            core_ebal_table = dassh.table.CoreEnergyBalanceTable()
            out += core_ebal_table.generate(self)

        # Coolant temperatures
        coolant_table = dassh.table.CoolantTempTable()
        out += coolant_table.generate(self)

        # Duct temperatures
        duct_table = dassh.table.DuctTempTable()
        out += duct_table.generate(self)

        # Peak pin temperatures
        if any(['pin' in a._peak.keys() for a in self.assemblies]):
            max_temps = dassh.table.PeakPinTempTable()
            out += max_temps.generate(self, 'clad', 'mw')
            out += max_temps.generate(self, 'fuel', 'cl')

        # Append to file
        with open(os.path.join(self.path, 'dassh.out'), 'a') as f:
            f.write(out)

########################################################################


def identify_asm(loc, id, dif3d_idx=True):
    """Identify assembly index and location depending on whether
    DIF3D or DASSH indexing is used

    Parameters
    ----------
    loc : tuple
        User-provided location: (ring, position)
    id : int
        User-provided assembly ID

    Returns
    -------

    """
    if dif3d_idx:   #
        dassh_loc = dassh.utils.dif3d_loc_to_dassh_loc(loc)
        dif3d_id = id
        dassh_id = None
    else:
        dassh_loc = loc
        dif3d_id = dassh.utils.dassh_loc_to_dif3d_id(loc)
        dassh_id = id
    return dif3d_id, dassh_id, dassh_loc


def get_rod_bundle_bnds(zfm, asm_data):
    """Determine axial bounds of Assembly rod bundle

    Parameters
    ----------
    zfm : numpy.ndarray
        Axial fine mesh points for the assembly power distribution
    asm_data : dict
        Dictionary describing assembly geometry

    Returns
    -------
    List
        Rod bundle lower and upper axial bounds (cm)

    """
    if asm_data.get('use_low_fidelity_model'):
        bundle_zbnd = [100 * zfm[-1], 100 * zfm[-1]]
    else:
        assert 'rods' in asm_data['AxialRegion'].keys()
        bundle_zbnd = [100 * asm_data['AxialRegion']['rods']['z_lo'],
                       100 * asm_data['AxialRegion']['rods']['z_hi']]
    return bundle_zbnd


def match_rodded_finemesh_bnds_dif3d(power_obj, asm_data):
    """Determine bounds of core rodded region"""
    if asm_data.get('use_low_fidelity_model'):
        ck_rod_bnds = [len(power_obj.z_mesh),
                       len(power_obj.z_mesh)]
    else:
        if 'rods' in asm_data['AxialRegion'].keys():
            ck_rod_bnds = [0, 0]
            ck_rod_bnds[0] = np.where(np.isclose(
                power_obj.z_mesh,
                100 * asm_data['AxialRegion']['rods']['z_lo']))[0][0]
            ck_rod_bnds[1] = np.where(np.isclose(
                power_obj.z_mesh,
                100 * asm_data['AxialRegion']['rods']['z_hi']))[0][0]
    k_bnds = [sum(power_obj.k_fints[:ck_rod_bnds[0]]),
              sum(power_obj.k_fints[:ck_rod_bnds[1]])]
    return k_bnds


def match_rodded_finemesh_bnds(zfm, asm_data):
    """Do it for user-specified power"""
    if asm_data.get('use_low_fidelity_model'):
        kbnds = [len(zfm), len(zfm)]
    else:
        assert 'rods' in asm_data['AxialRegion'].keys()
        kbnds = [0, 0]
        zlo = asm_data['AxialRegion']['rods']['z_lo']
        zhi = asm_data['AxialRegion']['rods']['z_hi']
        kbnds[0] = np.where(np.isclose(zfm, 100 * zlo))[0][0]
        kbnds[1] = np.where(np.isclose(zfm, 100 * zhi))[0][0]
    return kbnds


def calc_power_VARIANT(input_data, working_dir, t_pt=0):
    """Calculate the power distributions from VARIANT

    Parameters
    ----------
    data : dict
        DASSH input data dictionary
    working_dir : str
        Path to current working directory

    Returns
    -------
    dict
        DASSH Power objects for each type of assembly in the problem;
        different objects are required because different assemblies
        can have different unrodded region specifications

    """
    cwd = os.getcwd()
    if working_dir != '':
        os.chdir(working_dir)

    # Identify VARPOW keys for fuel and coolant
    fuel_id = _FUELS[input_data['Core']['fuel_material'].lower()]
    if type(fuel_id) == dict:
        fuel_id = fuel_id[input_data['Core']['fuel_alloy'].lower()]

    coolant_heating = input_data['Core']['coolant_heating']
    if coolant_heating is None:
        coolant_heating = input_data['Core']['coolant_material']
    if coolant_heating.lower() not in _COOLANTS.keys():
        module_logger.error('Unknown coolant specification for '
                            'heating calculation; must choose '
                            'from options: Na, NaK, Pb, Pb-Bi')
    else:
        cool_id = _COOLANTS[coolant_heating.lower()]

    # Run VARPOW, rename output files
    path2varpow = os.path.dirname(os.path.abspath(__file__))
    if sys.platform == 'darwin':
        path2varpow = os.path.join(path2varpow, 'varpow_osx.x')
    elif 'linux' in sys.platform:
        path2varpow = os.path.join(path2varpow, 'varpow_linux.x')
    else:
        raise SystemError('DASSH currently supports only Linux and OSX')
    with open('varpow_stdout.txt', 'w') as f:
        subprocess.call([path2varpow,
                         str(fuel_id),
                         str(cool_id),
                         input_data['Neutronics']['pmatrx'][t_pt],
                         input_data['Neutronics']['geodst'][t_pt],
                         input_data['Neutronics']['ndxsrf'][t_pt],
                         input_data['Neutronics']['znatdn'][t_pt],
                         input_data['Neutronics']['nhflux'][t_pt],
                         input_data['Neutronics']['ghflux'][t_pt]],
                        stdout=f)
    subprocess.call(['mv', 'MaterialPower.out',
                     'varpow_MatPower.out'])
    subprocess.call(['mv', 'VariantMonoExponents.out',
                     'varpow_MonoExp.out'])
    subprocess.call(['mv', 'Output.VARPOW', 'VARPOW.out'])

    os.chdir(cwd)
    return import_power_VARIANT(input_data, working_dir, t_pt)


def import_power_VARIANT(data, w_dir, t_pt=0):
    """Import power distributions from VARIANT

    Parameters
    ----------
    data : dict
        DASSH input data dictionary
    w_dir : str
        Path to current working directory
    t_pt (optional) : int
        If multiple CCCC file sets provided, indicate which to use
        (default = 0)

    Returns
    -------
    dict
        DASSH Power objects for each type of assembly in the problem;
        different objects are required because different assemblies
        can have different unrodded region specifications

    """
    # Create DASSH Power object
    # core_power = dassh.power.Power(
    #     os.path.join(w_dir, 'varpow_MatPower.out'),
    #     os.path.join(w_dir, 'varpow_MonoExp.out'),
    #     os.path.join(w_dir, 'VARPOW.out'),
    #     os.path.join(w_dir, data['Neutronics']['geodst'][t_pt]),
    #     user_power=data['Core']['total_power'],
    #     scalar=data['Core']['power_scaling_factor'],
    #     model=data['Core']['power_model'])
    core_power = dassh.power.Power(
        os.path.join(w_dir, 'varpow_MatPower.out'),
        os.path.join(w_dir, 'varpow_MonoExp.out'),
        os.path.join(w_dir, 'VARPOW.out'),
        os.path.join(w_dir, data['Neutronics']['geodst'][t_pt]),
        model=data['Core']['power_model'])

    # Raise negative power warning
    # negative_power = list(core_power.values())[0].negative_power
    negative_power = core_power.negative_power
    if negative_power < 0.0:  # Note: level 30 is "warning"
        module_logger.log(30, 'Negative powers found and set equal '
                              'to zero. Check flux solution for '
                              'convergence.')
        module_logger.log(30, 'Total negative power (W): '
                              + '{:0.3e}'.format(negative_power))

    return core_power


def approximate_temps(x, y, x_new, asm_lstsq_params=None, order=2):
    """Approximate a vector of temperatures to a coarser or finer mesh

    Parameters
    ----------
    x : numpy.ndarray
        The positions of the original mesh centroids along a hex
        side; length must be greater than 1
    y : numpy.ndarray
        The original temperatures at those centroids; length must
        be greater than 1
    x_new : numpy.ndarray
        The positions of the new mesh centroids to which the new
        temperatures will be approximated
    asm_lstsq_params : dict (optional)
        If provided, can bypass the setup portions of the Legendre
        polynomial fit to data
    order : int (optional)
        Order of the Legendre polynomial basis (default = 2)

    Returns
    -------
    numpy.ndarray
        The approximated temperatures at positions x_new

    Notes
    -----
    Used in DASSH to deal with mesh disagreement in the interassembly
    gap between assemblies with different number of pins

    """
    # If no interpolation needed, just return the original array
    if np.array_equal(x, x_new):
        return y

    # Otherwise...
    # Dress up the temperatures for the interpolation
    ym = copy.deepcopy(y)
    ym.shape = (6, int(ym.shape[0] / 6))
    to_append = np.roll(ym[:, -1], 1)
    to_append.shape = (6, 1)
    ym = np.hstack((to_append, ym))

    # If len(x_old) == 2 (only corners): No need for legendre fit!
    # The approximation is just a linear fit between the two corners
    if len(x) == 2:
        y_new = np.linspace(ym[:, 0], ym[:, 1], len(x_new))
        y_new = y_new.transpose()

    # If len(x_new) == 2 (only corners): No need for legendre fit!
    # Can just return the corner temperatures and be done with it.
    elif len(x_new) == 2:
        y_new = ym[:, (0, -1)]

    # Otherwise, bummer: you have to fit polynomial and generate
    # approximate values on the new mesh x points
    else:
        # y_new = np.zeros((6, len(x_new)))
        # for side in range(6):
        #     coeff = np.polynomial.legendre.legfit(x, ym[side], order)
        #     y_new[side] = np.polynomial.legendre.legval(x_new, coeff)

        if asm_lstsq_params is None:
            coeff = np.polynomial.legendre.legfit(x, ym.transpose(), 2)
        else:
            c, resids, rank, s = np.linalg.lstsq(
                asm_lstsq_params['lhs_over_scl'],
                ym.T,
                asm_lstsq_params['rcond'])
            coeff = (c.T / asm_lstsq_params['scl']).T
        y_new = np.polynomial.legendre.legval(x_new, coeff)

        # Use the exact corner temps from original array
        y_new[:, -1] = ym[:, -1]

    # Get rid of the stuff you added and return the flattened array
    y_new = y_new[:, 1:]
    return y_new.flatten()


def err_cb(error):
    raise error

########################################################################