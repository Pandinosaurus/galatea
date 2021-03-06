import sys
import theano.sandbox.rng_mrg
RandomStreams = theano.sandbox.rng_mrg.MRG_RandomStreams
from pylearn2.utils import serial
from pylearn2.config import yaml_parse
import theano.tensor as T
from theano import function
from pylearn2.gui.patch_viewer import make_viewer
import numpy as np

ignore, model_path = sys.argv

model = serial.load(model_path)
dbm = model
dataset = yaml_parse.load(model.dataset_yaml_src)

theano_rng = RandomStreams(42)

X = T.matrix()
X.tag.test_value = np.zeros((2,dbm.rbms[0].nvis),dtype=X.dtype)
H = T.matrix()
H.tag.test_value = np.zeros((2,dbm.rbms[0].nhid),dtype=X.dtype)
ip = dbm.inference_procedure
vhW = dbm.W[0]
gaussian = hasattr(dbm,'beta')
if gaussian:
    vhW = vhW * dbm.beta.dimshuffle(0,'x')
Hprime = theano_rng.binomial(
        size = H.shape,
        n = 1,
        dtype = H.dtype,
        p = ip.infer_H_hat_one_sided(
            other_H_hat = X,
            W = vhW,
            b = dbm.bias_hid[0]))
Xprime = theano_rng.binomial(
        size = X.shape,
        n = 1,
        dtype = X.dtype,
        p = ip.infer_H_hat_one_sided(
            other_H_hat = Hprime,
            W = dbm.W[0].T,
            b = dbm.bias_vis))
if gaussian:
    Xprime = theano_rng.normal(
            size = X.shape,
            avg = T.dot(Hprime, dbm.W[0].T) + \
                 dbm.bias_vis,
            std = 1./dbm.beta,
            dtype = X.dtype)

f = function([H,X],[Hprime,Xprime])

if ip.layer_schedule is None:
    ip.layer_schedule = [0] * 1
obs = ip.infer(X)
pH , = obs['H_hat']
sample_from_posterior = function([X],
        theano_rng.binomial( size = pH.shape,
            p = pH, n = 1, dtype = pH.dtype)  )


m = 100
X = dataset.get_batch_design(m)
H = sample_from_posterior(X)
assert len(dbm.rbms) == 1

while True:
    V = dataset.adjust_for_viewer(X)
    viewer = make_viewer(V, is_color = X.shape[1] % 3 == 0)
    viewer.show()

    print 'Waiting...'
    x = raw_input()
    if x == 'q':
        break
    print 'Running...'

    num_updates = 1

    try:
        num_updates = int(x)
    except:
        pass

    for i in xrange(num_updates):
        H,X = f(H,X)

