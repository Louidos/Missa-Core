import missa


def test_paket_hat_version():
    assert isinstance(missa.__version__, str)
    assert missa.__version__.count(".") == 2
