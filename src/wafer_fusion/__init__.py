"""Domain data processing API; synthetic offline baseline."""
from .service import process
from .calibration import process_calibrated

__all__ = ['process', 'process_calibrated']
