from pylearn2.costs.cost import Cost
import theano
from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams
import theano.tensor as T
from theano.printing import Print
import numpy as np
import warnings
from pylearn2.utils import block_gradient

class DBM_Inpaint_Binary(Cost):
    def __init__(self,
                    n_iter,
                    mask_gen = None,
                    inpaint_penalty = 1.,
                    weight_decay = None,
                    balance = False,
                    reweight = True,
                    reweight_correctly = False,
                    recons_penalty = None,
                    h_target = None,
                    h_penalty = None,
                    g_target = None,
                    g_penalty = None,
                    h_bimodality_penalty = None,
                    g_bimodality_penalty = None,
                    h_contractive_penalty = None,
                    g_contractive_penalty = None
                    ):
        self.__dict__.update(locals())
        del self.self
        assert not (reweight and reweight_correctly)


    def get_monitoring_channels(self, model, X, Y = None, drop_mask = None):

        d = self(model, X = X, drop_mask = drop_mask, return_locals = True)

        H_hat = d['H_hat']
        G_hat = d['G_hat']

        h = H_hat.mean(axis=0)
        g = G_hat.mean(axis=0)

        rval = {}

        rval['h_min'] = h.min()
        rval['h_mean'] = h.mean()
        rval['h_max'] = h.max()

        rval['g_min'] = g.min()
        rval['g_mean'] = g.mean()
        rval['g_max'] = g.max()

        h_max = H_hat.max(axis=0)
        h_min = H_hat.min(axis=0)
        g_max = G_hat.max(axis=0)
        g_min = G_hat.min(axis=0)

        h_range = h_max - h_min
        g_range = g_max - g_min

        rval['h_range_min'] = h_range.min()
        rval['h_range_mean'] = h_range.mean()
        rval['h_range_max'] = h_range.max()

        rval['g_range_min'] = g_range.min()
        rval['g_range_mean'] = g_range.mean()
        rval['g_range_max'] = g_range.max()

        return rval

    def __call__(self, model, X, drop_mask = None, return_locals = False):

        if not hasattr(model,'cost'):
            model.cost = self
        if not hasattr(model,'mask_gen'):
            model.mask_gen = self.mask_gen

        dbm = model
        assert len(dbm.rbms) == 2

        ip = dbm.inference_procedure

        if drop_mask is None:
            drop_mask = self.mask_gen(X)
        else:
            assert self.mask_gen is None

        gaussian = hasattr(model,'beta')

        vhW = dbm.W[0]

        if gaussian:
            #note: this is faster when batch_size > num G
            #if num G > batch_size, it's faster to multiply v by beta
            assert vhW.ndim == 2
            vhW = dbm.beta.dimshuffle(0,'x') * vhW
            assert vhW.ndim == 2

        if not gaussian:
            X_hat = X * (1-drop_mask) + drop_mask * T.nnet.sigmoid(dbm.bias_vis)
        else:
            X_hat = X * (1-drop_mask) + drop_mask * dbm.bias_vis

        X_hat = block_gradient(X_hat)

        H_hat = ip.infer_H_hat_one_sided(
                    other_H_hat = X_hat,
                    W = vhW * 2.,
                    b = dbm.bias_hid[0])
        G_hat = ip.infer_H_hat_one_sided(
                    other_H_hat = H_hat,
                    W = dbm.W[1],
                    b = dbm.bias_hid[-1])

        history = []

        def update_history():
            history.append( { 'X_hat' : X_hat, 'H_hat' : H_hat, 'G_hat' : G_hat } )

        update_history()

        for i in xrange(self.n_iter-1):
            H_hat = ip.infer_H_hat_two_sided(
                    H_hat_below = X_hat,
                    H_hat_above = G_hat,
                    W_below = vhW,
                    W_above = dbm.W[1],
                    b = dbm.bias_hid[0])

            unmasked = ip.infer_H_hat_one_sided(
                        other_H_hat = H_hat,
                        W = dbm.W[0].T,
                        b = dbm.bias_vis)

            if gaussian:
                unmasked = T.dot(H_hat, dbm.W[0].T) + dbm.bias_vis

            X_hat = X * (1-drop_mask)+drop_mask * unmasked

            G_hat = ip.infer_H_hat_one_sided(
                        other_H_hat = H_hat,
                        W = dbm.W[1],
                        b = dbm.bias_hid[-1])

            update_history()


        arg_to_sigmoid = T.dot(H_hat, dbm.W[0].T) + dbm.bias_vis
        #cross entropy is
        # - ( x log y + (1-x) log (1-y) )
        # - ( x log sigmoid( z) + (1-x) log sigmoid ( - z) )
        # x softplus( - z ) + (1-x) softplus ( z )

        unmasked_cost = X * T.nnet.softplus( - arg_to_sigmoid) + (1.-X) * T.nnet.softplus( arg_to_sigmoid)

        if gaussian:
            unmasked_cost = 0.5 * dbm.beta * T.sqr(X-T.dot(H_hat,dbm.W[0].T)-dbm.bias_vis) \
                    - 0.5 * T.log(dbm.beta / (2*np.pi))

        if gaussian:
            warnings.warn("might want to check that just using p(v|h) is the right thing to do, variationally")
            #ie, is the precision still beta, etc.?

        masked_cost = drop_mask * unmasked_cost

        if self.reweight:
            #this gives equal weight to each example
            ave_cost = masked_cost.sum() / drop_mask.sum()
        elif self.reweight_correctly:
            ave_cost = masked_cost.sum(axis=1) / T.maximum(1,drop_mask.sum(axis=1))
            ave_cost = ave_cost.mean()
        else:
            #this gives equal weight to each pixel, like in the pseudolikelihood cost
            ave_cost = masked_cost.mean()

        if not hasattr(self, 'inpaint_penalty'):
                self.inpaint_penalty = 1.0

        if self.inpaint_penalty == 0.0:
            ave_cost = 0.0
        else:
            ave_cost *= self.inpaint_penalty

        if self.h_target is not None and self.h_target != 0.0:
            ave_cost = ave_cost + \
                    self.h_penalty * abs(H_hat.mean(axis=0) - self.h_target).mean()

        if self.g_target is not None and self.g_penalty != 0.0:
            ave_cost = ave_cost + \
                    self.g_penalty * abs(G_hat.mean(axis=0) - self.g_target).mean()

        if self.weight_decay is not None and self.weight_decay != 0.0:
            for W in model.W:
                ave_cost = ave_cost + \
                    self.weight_decay * T.sqr(W).mean()

        if not hasattr(self,'recons_penalty'):
            self.recons_penalty = None
        if self.recons_penalty is not None and self.recons_penalty != 0.0:
            H_hat = ip.infer_H_hat_one_sided(
                        other_H_hat = X,
                        W = dbm.W[0] * 2.,
                        b = dbm.bias_hid[0])
            G_hat = ip.infer_H_hat_one_sided(
                        other_H_hat = H_hat,
                        W = dbm.W[1],
                        b = dbm.bias_hid[-1])
            for i in xrange(self.n_iter-1):
                H_hat = ip.infer_H_hat_two_sided(
                        H_hat_below = X,
                        H_hat_above = G_hat,
                        W_below = vhW,
                        W_above = dbm.W[1],
                        b = dbm.bias_hid[0])

                G_hat = ip.infer_H_hat_one_sided(
                            other_H_hat = H_hat,
                            W = dbm.W[1],
                            b = dbm.bias_hid[-1])

            arg_to_sigmoid = T.dot(H_hat, dbm.W[0].T) + dbm.bias_vis
            #cross entropy is
            # - ( x log y + (1-x) log (1-y) )
            # - ( x log sigmoid( z) + (1-x) log sigmoid ( - z) )
            # x softplus( - z ) + (1-x) softplus ( z )

            recons_cost = X * T.nnet.softplus( - arg_to_sigmoid) + (1.-X) * T.nnet.softplus( arg_to_sigmoid)

            if gaussian:
                recons_cost = 0.5 * dbm.beta * T.sqr( X - T.dot(H_hat, dbm.W[0].T) - dbm.bias_vis) - 0.5 * T.log(dbm.beta / (2.*np.pi))

            ave_cost = ave_cost + self.recons_penalty * recons_cost.mean()

        if not hasattr(self,'h_bimodality_penalty'):
            self.h_bimodality_penalty = None
            self.g_bimodality_penalty = None

        if self.h_bimodality_penalty is not None and self.h_bimodality_penalty != 0.0:
            h_bimodality_cost = self.h_bimodality_penalty * ( H_hat * (1-H_hat)).mean()
            ave_cost += h_bimodality_cost

        if self.g_bimodality_penalty is not None and self.g_bimodality_penalty != 0.0:
            g_bimodality_cost = self.g_bimodality_penalty * ( G_hat * (1-G_hat)).mean()
            ave_cost += g_bimodality_cost

        if not hasattr(self,'h_contractive_penalty'):
            self.h_contractive_penalty = None
            self.g_contractive_penalty = None

        if self.h_contractive_penalty is not None and self.h_contractive_penalty != 0.0:
            #we want to be sure H_hat came from encoding, not inpainting
            assert self.recons_penalty is not None and self.recons_penalty > 0.0
            left_factor = T.sqr(H_hat*(1-H_hat))
            right_factor_1 = T.sqr(vhW).sum(axis=0)
            right_factor_2 = T.sqr(dbm.W[1]).sum(axis=1)
            ave_cost += self.h_contractive_penalty * ( T.dot(left_factor, right_factor_1 + right_factor_2) ).mean() / float(model.rbms[0].nhid)

        if self.g_contractive_penalty is not None and self.g_contractive_penalty != 0.0:
            #we want to be sure G_hat came from encoding, not inpainting
            assert self.recons_penalty is not None and self.recons_penalty > 0.0
            left_factor = T.sqr(G_hat*(1-G_hat))
            right_factor = T.sqr(dbm.W[1]).sum(axis=0)
            ave_cost += self.h_contractive_penalty * T.dot(left_factor, right_factor).mean() / float(model.rbms[1].nhid)


        if return_locals:
            return locals()

        return ave_cost

class RBM_Inpaint_Binary(Cost):
    def __init__(self,
                    n_iter,
                    mask_gen = None,
                    weight_decay = None,
                    balance = False,
                    reweight = True,
                    reweight_correctly = False,
                    h_target = None,
                    h_penalty = None,
                    ):
        self.__dict__.update(locals())
        del self.self
        assert not (reweight and reweight_correctly)


    def get_monitoring_channels(self, model, X, Y = None, drop_mask = None):

        d = self(model, X = X, drop_mask = drop_mask, return_locals = True)

        H_hat = d['H_hat']

        h = H_hat.mean(axis=0)

        rval = {}

        rval['h_min'] = h.min()
        rval['h_mean'] = h.mean()
        rval['h_max'] = h.max()

        h_max = H_hat.max(axis=0)
        h_min = H_hat.min(axis=0)

        h_range = h_max - h_min

        rval['h_range_min'] = h_range.min()
        rval['h_range_mean'] = h_range.mean()
        rval['h_range_max'] = h_range.max()

        return rval

    def __call__(self, model, X, drop_mask = None, return_locals = False):

        assert not hasattr(model, 'beta') # this is only for binary for now
        if not hasattr(model,'cost'):
            model.cost = self
        if not hasattr(model,'mask_gen'):
            model.mask_gen = self.mask_gen

        dbm = model
        assert len(dbm.rbms) == 1

        ip = dbm.inference_procedure

        if drop_mask is None:
            drop_mask = self.mask_gen(X)
        else:
            assert self.mask_gen is None


        vhW = dbm.W[0]

        X_hat = X * (1-drop_mask) + drop_mask * T.nnet.sigmoid(dbm.bias_vis)

        H_hat = ip.infer_H_hat_one_sided(
                    other_H_hat = X_hat,
                    W = vhW,
                    b = dbm.bias_hid[0])

        history = []

        def update_history():
            history.append( { 'X_hat' : X_hat, 'H_hat' : H_hat } )

        update_history()

        for i in xrange(self.n_iter-1):
            H_hat = ip.infer_H_hat_one_sided(
                    other_H_hat = X_hat,
                    W = vhW,
                    b = dbm.bias_hid[0])

            unmasked = ip.infer_H_hat_one_sided(
                        other_H_hat = H_hat,
                        W = dbm.W[0].T,
                        b = dbm.bias_vis)

            X_hat = X * (1-drop_mask)+drop_mask * unmasked

            update_history()


        arg_to_sigmoid = T.dot(H_hat, dbm.W[0].T) + dbm.bias_vis
        #cross entropy is
        # - ( x log y + (1-x) log (1-y) )
        # - ( x log sigmoid( z) + (1-x) log sigmoid ( - z) )
        # x softplus( - z ) + (1-x) softplus ( z )

        unmasked_cost = X * T.nnet.softplus( - arg_to_sigmoid) + (1.-X) * T.nnet.softplus( arg_to_sigmoid)

        masked_cost = drop_mask * unmasked_cost

        if self.reweight:
            #this gives equal weight to each example
            ave_cost = masked_cost.sum() / drop_mask.sum()
        elif self.reweight_correctly:
            ave_cost = masked_cost.sum(axis=1) / T.maximum(1,drop_mask.sum(axis=1))
            ave_cost = ave_cost.mean()
        else:
            #this gives equal weight to each pixel, like in the pseudolikelihood cost
            ave_cost = masked_cost.mean()

        if not hasattr(self, 'inpaint_penalty'):
                self.inpaint_penalty = 1.0

        if self.h_target is not None and self.h_target != 0.0:
            ave_cost = ave_cost + \
                    self.h_penalty * abs(H_hat.mean(axis=0) - self.h_target).mean()

        if self.weight_decay is not None and self.weight_decay != 0.0:
            for W in model.W:
                ave_cost = ave_cost + \
                    self.weight_decay * T.sqr(W).mean()

        if return_locals:
            return locals()

        return ave_cost

def expected_energy(V_hat, H_hat, model):
    v_bias = - T.dot(V_hat, model.bias_vis)
    assert v_bias.ndim == 1
    assert H_hat.ndim == 2
    h_bias = - T.dot(H_hat, model.bias_hid[0])
    assert h_bias.ndim == 1
    weights = (T.dot(V_hat, model.W[0]) * H_hat).sum(axis=1)
    assert weights.ndim == 1
    rval = v_bias + h_bias + weights
    assert rval.ndim == 1
    return rval

def H_entropy_term(H_hat):
    z , = H_hat.owner.inputs
    log_H_hat = -T.nnet.softplus(-z)
    one_minus_H_hat = 1 - H_hat
    log_one_minus_H_hat = -T.nnet.softplus(z)

    rval = H_hat * log_H_hat + one_minus_H_hat * log_one_minus_H_hat
    rval = rval.sum(axis=1)
    assert rval.ndim == 1
    return rval

def V_entropy_term(unmasked, mask):
    z ,= unmasked.owner.inputs

    H_hat = unmasked
    log_H_hat = -T.nnet.softplus(-z)
    one_minus_H_hat = 1 - H_hat
    log_one_minus_H_hat = -T.nnet.softplus(z)

    rval = H_hat * log_H_hat + one_minus_H_hat * log_one_minus_H_hat
    assert rval.ndim == 2
    rval = (rval * mask).sum(axis=1)
    assert rval.ndim == 1
    return rval

class RBM_Bad_Variational(Cost):
    def __init__(self,
                    n_iter,
                    mask_gen = None,
                    weight_decay = None,
                    balance = False,
                    reweight = True,
                    reweight_correctly = False,
                    h_target = None,
                    h_penalty = None,
                    ):
        self.__dict__.update(locals())
        del self.self
        assert not (reweight and reweight_correctly)


    def get_monitoring_channels(self, model, X, Y = None, drop_mask = None):

        d = self(model, X = X, drop_mask = drop_mask, return_locals = True)

        main_H_hat = d['main_H_hat']
        H_hat_1 = d['H_hat_1']
        H_hat_2 = d['H_hat_2']
        rval = {}

        def add_H_channels(H_hat, prefix):
            h = H_hat.mean(axis=0)

            rval[prefix+'h_min'] = h.min()
            rval[prefix+'h_mean'] = h.mean()
            rval[prefix+'h_max'] = h.max()

            h_max = H_hat.max(axis=0)
            h_min = H_hat.min(axis=0)

            h_range = h_max - h_min

            rval[prefix+'h_range_min'] = h_range.min()
            rval[prefix+'h_range_mean'] = h_range.mean()
            rval[prefix+'h_range_max'] = h_range.max()

        add_H_channels(main_H_hat,'main_')
        add_H_channels(H_hat_1,'Q1_')
        add_H_channels(H_hat_2,'Q2_')

        return rval

    def __call__(self, model, X, drop_mask = None, return_locals = False):

        assert not hasattr(model, 'beta') # this is only for binary for now
        if not hasattr(model,'cost'):
            model.cost = self
        if not hasattr(model,'mask_gen'):
            model.mask_gen = self.mask_gen

        dbm = model
        assert len(dbm.rbms) == 1

        ip = dbm.inference_procedure

        if drop_mask is None:
            drop_mask = self.mask_gen(X)
        else:
            assert self.mask_gen is None

        vhW = dbm.W[0]


        main_H_hat = ip.infer_H_hat_one_sided(
                    other_H_hat = X,
                    W = vhW,
                    b = dbm.bias_hid[0])

        X_hat_1 = X * (1-drop_mask) + drop_mask * T.nnet.sigmoid(dbm.bias_vis)
        X_hat_2 = X * drop_mask + (1-drop_mask) * T.nnet.sigmoid(dbm.bias_vis)

        H_hat_1 = ip.infer_H_hat_one_sided(
                    other_H_hat = X_hat_1,
                    W = vhW,
                    b = dbm.bias_hid[0])
        H_hat_2 = ip.infer_H_hat_one_sided(
                    other_H_hat = X_hat_2,
                    W = vhW,
                    b = dbm.bias_hid[0])

        history = []

        def update_history():
            history.append( { 'X_hat' : X_hat_1, 'H_hat' : H_hat_1 } )

        update_history()

        for i in xrange(self.n_iter-1):
            H_hat_1 = ip.infer_H_hat_one_sided(
                        other_H_hat = X_hat_1,
                        W = vhW,
                        b = dbm.bias_hid[0])
            H_hat_2 = ip.infer_H_hat_one_sided(
                        other_H_hat = X_hat_2,
                        W = vhW,
                        b = dbm.bias_hid[0])


            unmasked_1 = ip.infer_H_hat_one_sided(
                        other_H_hat = H_hat_1,
                        W = dbm.W[0].T,
                        b = dbm.bias_vis)

            X_hat_1 = X * (1-drop_mask)+drop_mask * unmasked_1

            unmasked_2 = ip.infer_H_hat_one_sided(
                        other_H_hat = H_hat_2,
                        W = dbm.W[0].T,
                        b = dbm.bias_vis)

            X_hat_2 = X * drop_mask+(1-drop_mask) * unmasked_2

            update_history()



        class ElemwiseNoGradient(theano.tensor.Elemwise):

            def connection_pattern(self, node):

                return [ [ False ] ]

            def grad(self, inputs, output_gradients):
                return [ theano.gradient.DisconnectedType()() ]

        block_gradient = ElemwiseNoGradient(theano.scalar.identity)
        main_H_hat = block_gradient(main_H_hat)
        H_hat_1 = block_gradient(H_hat_1)
        H_hat_2 = block_gradient(H_hat_2)


        main_energy = expected_energy(X, main_H_hat, model)
        assert main_energy.ndim == 1
        denom_energy_1 = expected_energy(X_hat_1, H_hat_1, model)
        assert denom_energy_1.ndim == 1
        denom_energy_2 = expected_energy(X_hat_2, H_hat_2, model)
        assert denom_energy_2.ndim == 1

        energy_terms = 2. * main_energy - denom_energy_1 - denom_energy_2
        assert energy_terms.ndim == 1



        entropy_terms = 2. * H_entropy_term(main_H_hat) - H_entropy_term(H_hat_1) - H_entropy_term(H_hat_2) \
                - V_entropy_term(unmasked_1, drop_mask) - V_entropy_term(unmasked_2, 1-drop_mask)
        assert entropy_terms.ndim == 1

        total_cost = energy_terms + entropy_terms
        assert total_cost.ndim == 1

        ave_cost = total_cost.mean()


        if self.h_target is not None and self.h_target != 0.0:
            ave_cost = ave_cost + \
                    self.h_penalty * abs(main_H_hat.mean(axis=0) - self.h_target).mean()

        if self.weight_decay is not None and self.weight_decay != 0.0:
            for W in model.W:
                ave_cost = ave_cost + \
                    self.weight_decay * T.sqr(W).mean()

        if return_locals:
            return locals()

        return ave_cost


class MaskGen:
    def __init__(self, drop_prob, balance, n_channels = 1):
        self.__dict__.update(locals())
        del self.self

    def __call__(self, X):
        assert X.dtype == 'float32'
        theano_rng = RandomStreams(20120712)

        if not hasattr(self,'n_channels'):
            self.n_channels = 1

        m = X.shape[0]
        n = X.shape[1] / self.n_channels

        if self.drop_prob > 0:
            p = self.drop_prob
        else:
            p = theano_rng.uniform(size = (X.shape[0],),
                low=0.0, high=1.0,  dtype= X.dtype).dimshuffle(0,'x')
        drop_mask = theano_rng.binomial(
                    size = (m,n),
                    p = p,
                    n = 1,
                    dtype = X.dtype)

        if self.balance:
            flip = theano_rng.binomial(
                    size = ( X.shape[0] ,),
                    p = 0.5,
                    n = 1,
                    dtype = X.dtype).dimshuffle(0,'x')
            drop_mask = (1-drop_mask)*flip + drop_mask * (1-flip)

        if self.n_channels > 1:
            drop_mask = T.concatenate( [ drop_mask] * self.n_channels, axis=1)

        return drop_mask