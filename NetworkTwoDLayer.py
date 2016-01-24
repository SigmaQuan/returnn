from NetworkHiddenLayer import _NoOpLayer
from cuda_implementation.MultiDirectionalTwoDLSTMOp import MultiDirectionalTwoDLSTMOpInstance
import theano
import theano.tensor as T
import numpy
from math import sqrt

forget_gate_initial_bias = 1.0
lambda_gate_initial_bias = 0.0

class TwoDLSTMLayer(_NoOpLayer):
  layer_class = "conv"

  def __init__(self, n_out, **kwargs):
    super(TwoDLSTMLayer, self).__init__(**kwargs)
    assert len(self.sources) == 1
    source = self.sources[0]
    n_in = source.attrs['n_out']

    b1 = self.create_and_add_bias(n_out)
    b2 = self.create_and_add_bias(n_out)
    b3 = self.create_and_add_bias(n_out)
    b4 = self.create_and_add_bias(n_out)

    W1, V_h1, V_v1 = self.create_and_add_2d_lstm_weights(n_in, n_out)
    W2, V_h2, V_v2 = self.create_and_add_2d_lstm_weights(n_in, n_out)
    W3, V_h3, V_v3 = self.create_and_add_2d_lstm_weights(n_in, n_out)
    W4, V_h4, V_v4 = self.create_and_add_2d_lstm_weights(n_in, n_out)

    X = source.output
    #we need a 4d tensor with layout (height, width, batch, feature)
    assert X.ndim == 4, X.ndim
    #TODO: later we need to get this from the below layer
    sizes = T.alloc(numpy.array([2, 5], dtype="float32"), (2, 5, 2))

    Y1, Y2, Y3, Y4 = MultiDirectionalTwoDLSTMOpInstance(X, W1, W2, W3, W4, V_h1, V_h2, V_h3, V_h4,
                                                        V_v1, V_v2, V_v3, V_v4, b1, b2, b3, b4, sizes)[:4]
    Y = 0.25 * (Y1 + Y2 + Y3 + Y4)

    self.output = Y
    #self.make_output(self.output)
    self.set_attr('n_out', n_out)

  def create_and_add_2d_lstm_weights(self, n, m):
    W, U, V = self.create_xavier_weights((n, 5 * m)), self.create_xavier_weights((m, 5 * m)), \
              self.create_xavier_weights((m, 5 * m))
    W = self.add_param(W)
    U = self.add_param(U)
    V = self.add_param(V)
    return W, U, V

  def create_xavier_weights(self, shape):
    p = shape[0] + numpy.prod(shape[1:])
    W = numpy.asarray(self.rng.uniform(low=-sqrt(6) / sqrt(p), high = sqrt(6) / sqrt(p), size=shape),
                           dtype=theano.config.floatX)
    return theano.shared(value=W, borrow=True, name='W_' + self.name)

  def create_and_add_bias(self, n_cells):
    b_val = numpy.zeros((5 * n_cells,), dtype=theano.config.floatX)
    b_val[1 * n_cells:2 * n_cells] = forget_gate_initial_bias
    b_val[2 * n_cells:3 * n_cells] = lambda_gate_initial_bias
    b = theano.shared(b_val, borrow=True, name="b_" + self.name)
    b = self.add_param(b)
    return b