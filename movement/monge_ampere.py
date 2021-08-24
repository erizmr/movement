import firedrake
from firedrake import PETSc
import ufl
import numpy as np
from movement.mover import Mover


__all__ = ["MongeAmpereMover", "monge_ampere"]


class MongeAmpereMover(Mover):
    # TODO: doc
    def __init__(self, mesh, monitor_function, **kwargs):
        if monitor_function is None:
            raise ValueError("Please supply a monitor function")

        # Collect parameters before calling super
        self.pseudo_dt = firedrake.Constant(kwargs.pop('pseudo_timestep', 0.1))
        self.bc = kwargs.pop('boundary_conditions', None)
        self.maxiter = kwargs.pop('maxiter', 1000)
        self.rtol = kwargs.pop('rtol', 1.0e-08)
        super().__init__(mesh, monitor_function=monitor_function)

        # Create function spaces
        self.P0 = firedrake.FunctionSpace(mesh, "DG", 0)
        self.P1 = firedrake.FunctionSpace(mesh, "CG", 1)
        self.P1_vec = firedrake.VectorFunctionSpace(mesh, "CG", 1)
        self.P1_ten = firedrake.TensorFunctionSpace(mesh, "CG", 1)

        # Create functions to hold solution data
        self.phi = firedrake.Function(self.P1)
        self.phi_old = firedrake.Function(self.P1)
        self.sigma = firedrake.Function(self.P1_ten)  # NOTE: initialised to zero
        self.sigma_old = firedrake.Function(self.P1_ten)

        # Create objects used during the mesh movement
        self.theta = firedrake.Constant(0.0)
        self.monitor = firedrake.Function(self.P1, name="Monitor function")
        self.monitor.interpolate(self.monitor_function(self.mesh))
        self.volume = firedrake.Function(self.P0, name="Mesh volume")
        self.volume.interpolate(ufl.CellVolume(mesh))
        self.original_volume = firedrake.Function(self.volume)
        self.total_volume = firedrake.assemble(firedrake.Constant(1.0)*self.dx)
        self.L_P0 = firedrake.TestFunction(self.P0)*self.monitor*self.dx
        self._grad_phi = firedrake.Function(self.P1_vec)
        self.grad_phi = firedrake.Function(self.mesh.coordinates)

        # Setup residuals
        I = ufl.Identity(self.dim)
        self.theta_form = self.monitor*ufl.det(I + self.sigma_old)*self.dx
        self.residual = self.monitor*ufl.det(I + self.sigma_old) - self.theta
        psi = firedrake.TestFunction(self.P1)
        self.residual_l2_form = psi*self.residual*self.dx
        self.norm_l2_form = psi*self.theta*self.dx

    @property
    def pseudotimestepper(self):
        # TODO: doc
        if hasattr(self, '_pseudotimestepper'):
            return self._pseudotimestepper
        phi = firedrake.TrialFunction(self.P1)
        psi = firedrake.TestFunction(self.P1)
        a = ufl.inner(ufl.grad(psi), ufl.grad(phi))*self.dx
        L = ufl.inner(ufl.grad(psi), ufl.grad(self.phi_old))*self.dx \
            + self.pseudo_dt*psi*self.residual*self.dx
        problem = firedrake.LinearVariationalProblem(a, L, self.phi)
        sp = {
            "ksp_type": "cg",
            "pc_type": "gamg",
        }
        nullspace = firedrake.VectorSpaceBasis(constant=True)
        self._pseudotimestepper = firedrake.LinearVariationalSolver(
            problem, solver_parameters=sp,
            nullspace=nullspace, transpose_nullspace=nullspace,
        )
        return self._pseudotimestepper

    @property
    def equidistributor(self):
        # TODO: doc
        if hasattr(self, '_equidistributor'):
            return self._equidistributor
        n = ufl.FacetNormal(self.mesh)
        sigma = firedrake.TrialFunction(self.P1_ten)
        tau = firedrake.TestFunction(self.P1_ten)
        a = ufl.inner(tau, sigma)*self.dx
        L = -ufl.dot(ufl.div(tau), ufl.grad(self.phi))*self.dx \
            + (tau[0, 1]*n[1]*self.phi.dx(0) + tau[1, 0]*n[0]*self.phi.dx(1))*self.ds
        problem = firedrake.LinearVariationalProblem(a, L, self.sigma)
        sp = {"ksp_type": "cg"}
        self._equidistributor = firedrake.LinearVariationalSolver(problem, solver_parameters=sp)
        return self._equidistributor

    @property
    def l2_projector(self):
        # TODO: doc
        if hasattr(self, '_l2_projector'):
            return self._l2_projector
        u_cts = firedrake.TrialFunction(self.P1_vec)
        v_cts = firedrake.TestFunction(self.P1_vec)

        # Domain interior
        a = ufl.inner(v_cts, u_cts)*self.dx
        L = ufl.inner(v_cts, ufl.grad(self.phi_old))*self.dx

        # Enforce no movement normal to boundary  TODO: generalise
        n = ufl.FacetNormal(self.mesh)
        bcs = []
        for i in self.mesh.exterior_facets.unique_markers:
            _n = [firedrake.assemble(n[j]*self.ds(i)) for j in range(self.dim)]
            if np.allclose(_n, 0.0):
                raise ValueError(f"Invalid normal vector {_n}")
            else:
                if self.dim != 2:
                    raise NotImplementedError  # TODO
                if np.isclose(_n[0], 0.0):
                    bcs.append(firedrake.DirichletBC(self.P1_vec.sub(1), 0, i))
                elif np.isclose(_n[1], 0.0):
                    bcs.append(firedrake.DirichletBC(self.P1_vec.sub(0), 0, i))
                else:
                    raise NotImplementedError("Non-axis-aligned geometries not considered yet.")

        # Create solver
        problem = firedrake.LinearVariationalProblem(a, L, self._grad_phi, bcs=bcs)
        sp = {"ksp_type": "cg"}
        self._l2_projector = firedrake.LinearVariationalSolver(problem, solver_parameters=sp)
        return self._l2_projector

    @property
    def x(self):
        # TODO: doc
        try:
            self.grad_phi.assign(self._grad_phi)
        except Exception:
            firedrake.par_loop(
                ('{[i, j] : 0 <= i < cg.dofs and 0 <= j < 2}', 'dg[i, j] = cg[i, j]'),
                self.dx,
                {'cg': (self._grad_phi, firedrake.READ), 'dg': (self.grad_phi, firedrake.WRITE)},
                is_loopy_kernel=True)
        self._x.assign(self.xi + self.grad_phi)  # x = ξ + grad(φ)
        return self._x

    @property
    def diagnostics(self):
        # TODO: doc
        v = self.volume.vector().gather()
        minmax = v.min()/v.max()
        mean = v.sum()/v.max()
        w = v.copy() - mean
        w *= w
        std = np.sqrt(w.sum()/w.size)
        equi = std/mean
        residual_l2 = firedrake.assemble(self.residual_l2_form).dat.norm
        norm_l2 = firedrake.assemble(self.norm_l2_form).dat.norm
        residual_l2_rel = residual_l2/norm_l2
        return minmax, residual_l2_rel, equi

    def adapt(self):
        maxiter = self.maxiter
        rtol = self.rtol
        for i in range(maxiter):

            # L2 project
            self.l2_projector.solve()

            # Update mesh coordinates
            self.mesh.coordinates.assign(self.x)

            # Update monitor function
            self.monitor.interpolate(self.monitor_function(self.mesh))
            firedrake.assemble(self.L_P0, tensor=self.volume)
            self.volume /= self.original_volume
            self.mesh.coordinates.assign(self.xi)

            # Evaluate normalisation coefficient
            self.theta.assign(firedrake.assemble(self.theta_form)/self.total_volume)

            # Check convergence criteria
            minmax, residual, equi = self.diagnostics
            if i == 0:
                initial_norm = residual
            PETSc.Sys.Print(f"{i:4d}"
                            f"   Min/Max {minmax:10.4e}"
                            f"   Residual {residual:10.4e}"
                            f"   Equidistribution {equi:10.4e}")
            if residual < rtol:
                PETSc.Sys.Print(f"Converged in {i+1} iterations.")
                break
            if residual > 2.0*initial_norm:
                raise firedrake.ConvergenceError(f"Diverged after {i+1} iterations.")

            # Apply pseudotimestepper and equidistributor
            self.pseudotimestepper.solve()
            self.equidistributor.solve()
            self.phi_old.assign(self.phi)
            self.sigma_old.assign(self.sigma)
        self.mesh.coordinates.assign(self.x)


def monge_ampere(mesh, monitor_function):
    # TODO: doc
    mover = MongeAmpereMover(mesh, monitor_function)
    mover.adapt()
