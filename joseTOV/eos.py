import os
import jax
import jax.numpy as jnp
from jax.scipy.special import factorial
from jaxtyping import Array, Float, Int, Bool

from . import utils, tov

# get the crust
DEFAULT_DIR = os.path.join(os.path.dirname(__file__))
# TODO: do we want several crust files or do we always use this crust? Perhaps store some crust files with correct format and units in the data folder?
BPS_CRUST_FILENAME = f"{DEFAULT_DIR}/crust/BPS.npz"


class Interpolate_EOS_model(object):
    """
    Base class to interpolate EOS data. 
    """
    def __init__(
        self,
        n: Float[Array, "n_points"],
        p: Float[Array, "n_points"],
        e: Float[Array, "n_points"],
    ):
        """
        Initialize the EOS model with the provided data and compute auxiliary data.

        Args:
            n (Float[Array, n_points]): Number densities. Expected units are n[fm^-3]
            p (Float[Array, n_points]): Pressure values. Expected units are p[MeV / fm^3]
            e (Float[Array, n_points]): Energy densities. Expected units are e[MeV / fm^3]
        """
        
        # Save the provided data as attributes, make conversions
        self.n = jnp.array(n * utils.fm_inv3_to_geometric)
        self.p = jnp.array(p * utils.MeV_fm_inv3_to_geometric)
        self.e = jnp.array(e * utils.MeV_fm_inv3_to_geometric)
        
        # Pre-calculate quantities
        self.logn = jnp.log(self.n)
        self.logp = jnp.log(self.p)
        self.h = utils.cumtrapz(self.p / (self.e + self.p), jnp.log(self.p)) # enthalpy
        self.loge = jnp.log(self.e)
        self.logh = jnp.log(self.h)
        
        # TODO: might be better to use jnp.gradient?
        dloge_dlogp = jnp.diff(self.loge) / jnp.diff(self.logp)
        dloge_dlogp = jnp.concatenate(
            (
                jnp.array(
                    [
                        dloge_dlogp.at[0].get(),
                    ]
                ),
                dloge_dlogp,
            )
        )
        self.dloge_dlogp = dloge_dlogp

    def energy_density_from_pseudo_enthalpy(self, h: Float):
        loge_of_h = jnp.interp(jnp.log(h), self.logh, self.loge)
        return jnp.exp(loge_of_h)

    def pressure_from_pseudo_enthalpy(self, h: Float):
        logp_of_h = jnp.interp(jnp.log(h), self.logh, self.logp)
        return jnp.exp(logp_of_h)

    def dloge_dlogp_from_pseudo_enthalpy(self, h: Float):
        return jnp.interp(h, self.h, self.dloge_dlogp)

    def pseudo_enthalpy_from_pressure(self, p: Float):
        logh_of_p = jnp.interp(jnp.log(p), self.logp, self.logh)
        return jnp.exp(logh_of_p)

    def pressure_from_number_density(self, n: Float):
        logp_of_n = jnp.interp(n, self.n, self.logp)
        return jnp.exp(logp_of_n)


class MetaModel_EOS_model(Interpolate_EOS_model):
    """
    MetaModel_EOS_model is a class to interpolate EOS data with a meta-model.

    Args:
        Interpolate_EOS_model (object): Base class of interpolation EOS data.
    """
    def __init__(
        self,
        coefficient_sat: Float[Array, "n_sat_coeff"],
        coefficient_sym: Float[Array, "n_sym_coeff"],
        nsat: Float=0.16,
        nmin: Float=0.1,
        nmax: Float=12 * 0.16,
        ndat_mm: Int=100,
        fix_proton_fraction: Bool=False,
        fix_proton_fraction_val: Float=0.0,
        crust_filename: str=BPS_CRUST_FILENAME,
        use_empty_crust: Bool=False,
        nmax_crust: Float=0.08,
        ndat_spline: Int=10,
    ):
        """
        Initialize the MetaModel_EOS_model with the provided coefficients and compute auxiliary data.
        
        Number densities are in unit of fm^-3. 
        
        Args:
            coefficient_sat (Float[Array, n_sat_coeff]): Array of coefficients for the saturation part of the EOS.
            coefficient_sym (Float[Array, n_sym_coeff]): Array of coefficients for the symmetry part of the EOS.
            nsat (float, optional): Value for the number saturation density. Defaults to 0.16, in [fm^-3].
            nmin (float, optional): Starting density from which the metamodel part of the EOS is constructed. Defaults to 0.1 fm^-3.
            nmax (float, optional): Maximum number density up to which EOS is constructed. Defaults to 12 * 0.16, i.e. 12 n_sat with n_sat = 0.16 fm^-3.
            ndat (int, optional): Number of datapoints used for the curves (logarithmically spaced). Defaults to 1000.
            fix_proton_fraction (bool, optional): If True, the proton fraction is fixed to a constant value. Defaults to False.
            fix_proton_fraction_val (float, optional): Value to which the proton fraction is fixed. Defaults to 0.0.    
            crust_filename (str, optional): Name of the crust file. Defaults to BPS_CRUST_FILENAME. Expected to be a .npz file with keys "n", "p", "e".
            use_empty_crust (bool, optional): If True, the crust data is not used. Defaults to False. TODO: check if useful or 
            nmax_crust (float, optional): Maximum number density for the crust data. Defaults to 0.08 fm^-3. Will interpolate with a spline to connect crust and metamodel.
            ndat_spline (float, optional): Number of datapoints used for the spline interpolation between the crust and the metamodel. Defaults to 10.
        """
        
        # Get the crust part:
        if use_empty_crust:
            ns_crust = jnp.array([])
            ps_crust = jnp.array([])
            es_crust = jnp.array([])
            
            self.nmax_crust = 0.0
            self.ns_crust = jnp.array([])
            self.cs2_crust = jnp.array([])
        else:
            crust = jnp.load(crust_filename)
            ns_crust = crust["n"]
            ps_crust = crust["p"]
            es_crust = crust["e"]
            
            # Make sure nmax_crust is not larger than the maximum number density in the crust data and below start of MM
            nmax_crust = min([nmax_crust, ns_crust[-1]])
            nmax_crust = min([nmax_crust, nmin])
            
            # Limit values to below nmax_crust
            mask = ns_crust < nmax_crust
            ns_crust = ns_crust[mask]
            ps_crust = ps_crust[mask]
            es_crust = es_crust[mask]
            
            # Compute the speed-of-sound (dp/de) for the crust:
            self.nmax_crust = nmax_crust
            cs2_crust = jnp.diff(ps_crust) / jnp.diff(es_crust)
            cs2_crust = jnp.append(jnp.array([1e-5]), cs2_crust)
            
            self.ns_crust = ns_crust
            self.cs2_crust = cs2_crust
            
        # add the first derivative coefficient in Esat to
        # make it work with jax.numpy.polyval
        coefficient_sat = jnp.insert(coefficient_sat, 1, 0.0)
        
        # Get the coefficents index array and get coefficients
        index_sat = jnp.arange(len(coefficient_sat))
        index_sym = jnp.arange(len(coefficient_sym))

        # Save as attributes
        self.coefficient_sat = coefficient_sat / factorial(index_sat)
        self.coefficient_sym = coefficient_sym / factorial(index_sym)
        self.nsat = nsat
        self.nmin = nmin
        self.fix_proton_fraction = fix_proton_fraction
        self.fix_proton_fraction_val = fix_proton_fraction_val
        
        # Compute n, p, e for the MetaModel (number densities in unit of fm^-3)
        ns_mm = jnp.logspace(jnp.log10(nmin), jnp.log10(nmax), num=ndat_mm)
        ps_mm = self.pressure_from_number_density_nuclear_unit(ns_mm)
        es_mm = self.energy_density_from_number_density_nuclear_unit(ns_mm)
        cs2_mm = self.cs2_from_number_density_nuclear_unit_metamodel(ns_mm)
        
        # Compute the c2s and make a smooth connection between crust and MM
        ns = jnp.concatenate((ns_crust, ns_mm))
        cs2 = jnp.concatenate((self.cs2_crust, cs2_mm))
        
        # Interpolate with a spline
        ns_spline = jnp.linspace(self.nmax_crust, nmin, num=ndat_spline)
        ps_spline = self.pressure_from_number_density_nuclear_unit(ns_spline)
        es_spline = self.energy_density_from_number_density_nuclear_unit(ns_spline)
        cs2_spline = utils.interpax_interp1d(ns_spline, ns, cs2)
        
        # Combine all:
        ns = jnp.concatenate((ns_crust, ns_spline, ns_mm))
        es = jnp.concatenate((es_crust, es_spline, es_mm))
        ps = jnp.concatenate((ps_crust, ps_spline, ps_mm))
        cs2 = jnp.concatenate((self.cs2_crust, cs2_spline, cs2_mm))
        
        # Initialize with parent class and save cs2
        super().__init__(ns, ps, es)
        self.cs2 = cs2

    def esym(self, n: Float[Array, "n_points"]):
        x = (n - self.nsat) / (3.0 * self.nsat)
        return jnp.polyval(self.coefficient_sym[::-1], x)

    def esat(self, n: Float[Array, "n_points"]):
        x = (n - self.nsat) / (3.0 * self.nsat)
        return jnp.polyval(self.coefficient_sat[::-1], x)

    def proton_fraction(self, n: Float[Array, "n_points"]) -> Float[Array, "n_points"]:
        """
        Get the proton fraction for a given number density. If proton fraction is fixed, return the fixed value.

        Args:
            n (Float[Array, "n_points"]): Number density in fm^-3.

        Returns:
            Float[Array, "n_points"]: Proton fraction as a function of the number density, either computed or the fixed value.
        """
        return jax.lax.cond(
                self.fix_proton_fraction,
                lambda x: self.fix_proton_fraction_val * jnp.ones(n.shape),
                self.compute_proton_fraction,
                n
            )
    
    def compute_proton_fraction(self, n: Float[Array, "n_points"]) -> Float[Array, "n_points"]:
        """
        Computes the proton fraction for a given number density.

        Args:
            n (Float[Array, "n_points"]): Number density in fm^-3.

        Returns:
            Float[Array, "n_points"]: Proton fraction as a function of the number density.
        """
        # chemical potential of electron
        # mu_e = hbarc * pow(3 * pi**2 * x * n, 1. / 3.)
        #      = hbarc * pow(3 * pi**2 * n, 1. / 3.) * y (y = x**1./3.)
        # mu_p - mu_n = dEdx
        #             = -4 * Esym * (1. - 2. * x)
        #             = -4 * Esym + 8 * Esym * y**3
        # at beta equilibrium, the polynominal is given by
        # mu_e(y) + dEdx(y) - (m_n - m_p) = 0
        # p_0 = -4 * Esym - (m_n - m_p)
        # p_1 = hbarc * pow(3 * pi**2 * n, 1. / 3.)
        # p_2 = 0
        # p_3 = 8 * Esym
        Esym = self.esym(n)
        
        a = 8.0 * Esym
        b = jnp.zeros(shape=n.shape)
        c = utils.hbarc * jnp.power(3.0 * jnp.pi**2 * n, 1.0 / 3.0)
        d = -4.0 * Esym - (utils.m_n - utils.m_p)
        
        coeffs = jnp.array(
            [
                a,
                b,
                c,
                d,
            ]
        ).T
        ys = utils.cubic_root_for_proton_fraction(coeffs)
        physical_ys = jnp.where(
            (ys.imag == 0.0) * (ys.real >= 0.0) * (ys.real <= 1.0),
            ys.real,
            jnp.zeros_like(ys.real),
        ).sum(axis=1)
        proton_fraction = jnp.cbrt(physical_ys)
        return proton_fraction

    def energy_per_particle_nuclear_unit(self, n: Float[Array, "n_points"]):
        proton_fraction = self.proton_fraction(n)
        delta = 1.0 - 2.0 * proton_fraction
        dynamic_part = self.esat(n) + self.esym(n) * (delta ** 2)
        static_part = proton_fraction * utils.m_p + (1.0 - proton_fraction) * utils.m_n
        
        return dynamic_part + static_part

    def energy_density_from_number_density_nuclear_unit(
        self, n: Float[Array, "n_points"]
    ):
        return n * self.energy_per_particle_nuclear_unit(n)

    def pressure_from_number_density_nuclear_unit(self, n: Float[Array, "n_points"]):
        p = n * n * jnp.diagonal(jax.jacfwd(self.energy_per_particle_nuclear_unit)(n))
        return p
    
    def cs2_from_number_density_nuclear_unit_metamodel(self, n: Float[Array, "n_points"], cs2_min: float = 1e-5) -> Float[Array, "n_points"]:
        """
        Compute the speed of sound squared from the number density in nuclear units in the metamodel part.

        Args:
            n (Float[Array, Float[Array, "n_points"]): Number density in fm^-3.
            cs2_min (float, optional): Minimal value to clip cs2 values computed. Defaults to 1e-3.

        Returns:
            Float[Array, "n_points"]: Speed of sound squared, clipped to be between [cs2_min, 1.0], and with the same size as the input n
        """
        
        # TODO: is this correct?
        p = self.pressure_from_number_density_nuclear_unit(n)
        e = self.energy_density_from_number_density_nuclear_unit(n)
        cs2 = jnp.diff(p) / jnp.diff(e)
        cs2 = jnp.clip(cs2, cs2_min, 1.0)
        # TODO: diff method reduces array size by 1, make sure same array size -- is this the best option right now? Perhaps use jnp.gradient?
        cs2 = jnp.concatenate(
            (
                jnp.array(
                    [
                        cs2.at[0].get(),
                    ]
                ),
                cs2,
            )
        )
        
        return cs2
    
    def cs2_from_number_density_nuclear_unit(self, n: Float[Array, "n_points"], cs2_min: float = 1e-5) -> Float[Array, "n_points"]:
        """
        Below nmin, i.e, the starting density of the MM, we return the evaluation of the cubic spline interpolator. Above nmin, we use the metamodel function. 
        
        Args:
            n (Float[Array, "n_points"]): Number density in fm^-3.
            cs2_min (float, optional): Minimal value to clip cs2 values computed. Defaults to 1e-3.

        Returns:
            _type_: _description_
        """
        # TODO: this is cumbersome... improve!
        ns = self.n / utils.fm_inv3_to_geometric
        
        cs2 = jnp.where(
            n <= self.nmin,
            utils.interpax_interp1d(n, ns, self.cs2),
            self.cs2_from_number_density_nuclear_unit_metamodel(n, cs2_min)
        )
        
        return cs2

class MetaModel_with_CSE_EOS_model(Interpolate_EOS_model):
    """
    MetaModel_with_CSE_EOS_model is a class to interpolate EOS data with a meta-model and using the CSE.

    Args:
        Interpolate_EOS_model (object): Base class of interpolation EOS data.
    """
    def __init__(
        self,
        # parameters for the MetaModel
        coefficient_sat: Float[Array, "n_sat_coeff"],
        coefficient_sym: Float[Array, "n_sym_coeff"],
        nbreak: Float,
        # parameters for the CSE
        ngrids: Float[Array, "n_grid_point"],
        cs2grids: Float[Array, "n_grid_point"],
        nsat: Float=0.16,
        nmin: Float=0.1,
        nmax: Float=12 * 0.16,
        ndat_metamodel: Int=1000,
        ndat_CSE: Int=1000,
        **metamodel_kwargs
    ):
        """
        Initialize the MetaModel_with_CSE_EOS_model with the provided coefficients and compute auxiliary data.

        Args:
            coefficient_sat (Float[Array, "n_sat_coeff"]): The coefficients for the saturation part of the metamodel part of the EOS.
            coefficient_sym (Float[Array, "n_sym_coeff"]): The coefficients for the symmetry part of the metamodel part of the EOS.
            nbreak (Float): The number density at the transition point between the metamodel and the CSE part of the EOS.
            ngrids (Float[Array, "n_grid_point"]): The number densities for the CSE part of the EOS.
            cs2grids (Float[Array, "n_grid_point"]): The speed of sound squared for the CSE part of the EOS.
            nsat (Float, optional): Saturation density. Defaults to 0.16 fm^-3.
            nmin (Float, optional): Starting point of densities. Defaults to 0.1 fm^-3.
            nmax (Float, optional): End point of EOS. Defaults to 12*0.16 fm^-3, i.e. 12 nsat.
            ndat_metamodel (Int, optional): Number of datapoints to be used for the metamodel part of the EOS. Defaults to 1000.
            ndat_CSE (Int, optional): Number of datapoints to be used for the CSE part of the EOS. Defaults to 1000.
        """

        # Initializate the MetaModel part up to n_break
        self.metamodel = MetaModel_EOS_model(
            coefficient_sat,
            coefficient_sym,
            nsat=nsat,
            nmin=nmin,
            nmax=nbreak,
            ndat=ndat_metamodel,
            **metamodel_kwargs
        )
        assert len(ngrids) == len(cs2grids), "ngrids and cs2grids must have the same length."
        # calculate the chemical potential at the transition point
        self.nbreak = nbreak
        
        # TODO: seems a bit cumbersome, can we simplify this?
        self.p_break = (
            self.metamodel.pressure_from_number_density_nuclear_unit(
                jnp.array(
                    [
                        self.nbreak,
                    ]
                )
            )
            .at[0]
            .get()
        )
        self.e_break = (
            self.metamodel.energy_density_from_number_density_nuclear_unit(
                jnp.array(
                    [
                        self.nbreak,
                    ]
                )
            )
            .at[0]
            .get()
        )
        
        # TODO: this has to be checked!
        self.mu_break = (self.p_break + self.e_break) / self.nbreak
        self.cs2_break = (
            jnp.diff(self.metamodel.p).at[-1].get()
            / jnp.diff(self.metamodel.e).at[-1].get()
        )
        # define the speed-of-sound interpolation
        # of the extension portion
        
        self.ngrids = jnp.concatenate((jnp.array([self.nbreak]), ngrids))
        self.cs2grids = jnp.concatenate((jnp.array([self.cs2_break]), cs2grids))
        self.cs2_function = lambda n: jnp.interp(n, self.ngrids, self.cs2grids)
        
        # Compute n, p, e for CSE (number densities in unit of fm^-3)
        ns = jnp.logspace(jnp.log10(self.nbreak), jnp.log10(nmax), num=ndat_CSE)
        mus = self.mu_break * jnp.exp(utils.cumtrapz(self.cs2_function(ns) / ns, ns))
        ps = self.p_break + utils.cumtrapz(self.cs2_function(ns) * mus, ns)
        es = self.e_break + utils.cumtrapz(mus, ns)
        
        # Combine metamodel and CSE data
        # TODO: converting units back and forth might be numerically unstable if conversion factors are large?
        ns = jnp.concatenate((self.metamodel.n / utils.fm_inv3_to_geometric, ns))
        ps = jnp.concatenate((self.metamodel.p / utils.MeV_fm_inv3_to_geometric, ps))
        es = jnp.concatenate((self.metamodel.e / utils.MeV_fm_inv3_to_geometric, es))

        super().__init__(ns, ps, es)
        
    def cs2_from_number_density_nuclear_unit(self, n: Float[Array, "n_points"], cs2_min: float = 1e-5) -> Float[Array, "n_points"]:
        """
        Compute the speed of sound squared from the number density in nuclear units. Uses the metamodel for densities below nbreak and the CSE for densities above nbreak.

        Args:
            n (Float[Array, "n_points"]): Number density in fm^-3.
            cs2_min (float, optional): Minimal value to clip cs2 values computed. Defaults to 1e-3.

        Returns:
            Float[Array, "n_points"]: Speed of sound squared, clipped to be between [cs2_min, 1.0], and with the same size as the input n
        """
        cs2 = jnp.where(n < self.nbreak, self.metamodel.cs2_from_number_density_nuclear_unit(n), self.cs2_function(n))
        cs2 = jnp.clip(cs2, cs2_min, 1.0)
        return cs2


def construct_family(eos: tuple[Float[Array, "n_points"], Float[Array, "n_points"], Float[Array, "n_points"], Float[Array, "n_points"], Float[Array, "n_points"]], 
                     ndat: Int=50, 
                     min_nsat: Float=2) -> tuple[Float[Array, "ndat"], Float[Array, "ndat"], Float[Array, "ndat"], Float[Array, "ndat"]]:
    """
    Solve the TOV equations and generate the M, R and Lambda curves.

    Args:
        eos (tuple[Float[Array, "n_points"], Float[Array, "n_points"], Float[Array, "n_points"], Float[Array, "n_points"], Float[Array, "n_points"]]): Tuple containing n, p, h, e and dloge_dlogp.
        ndat (int, optional): Number of datapoints used when constructing the central pressure grid. Defaults to 50.
        min_nsat (int, optional): Starting density for central pressure in numbers of nsat (assumed to be 0.16 fm^-3). Defaults to 2.

    Returns:
        tuple[Float[Array, "ndat"], Float[Array, "ndat"], Float[Array, "ndat"], Float[Array, "ndat"]]: log(pcs), masses in solar masses, radii in km, and dimensionless tidal deformabilities
    """
    # Construct the dictionary
    # TODO: does this have to be a tuple? For typing and docs, might be better to give as separate arguments?
    ns, ps, hs, es, dloge_dlogps = eos
    eos_dict = dict(p=ps, h=hs, e=es, dloge_dlogp=dloge_dlogps)
    
    # calculate the pc_min
    pc_min = utils.interp_in_logspace(min_nsat * 0.16 * utils.fm_inv3_to_geometric, ns, ps)

    # end at pc at pmax
    pc_max = eos_dict["p"][-1]

    pcs = jnp.logspace(jnp.log10(pc_min), jnp.log10(pc_max), num=ndat)

    # TODO: why vectorize, and not jax.vmap?
    ms, rs, ks = jnp.vectorize(
        tov.tov_solver,
        excluded=[
            0,
        ],
    )(eos_dict, pcs)

    # calculate the compactness
    cs = ms / rs

    # convert the mass to solar mass
    ms /= utils.solar_mass_in_meter
    # convert the radius to km
    rs /= 1e3

    # calculate the tidal deformability
    lambdas = 2.0 / 3.0 * ks * jnp.power(cs, -5.0)

    return jnp.log(pcs), ms, rs, lambdas
