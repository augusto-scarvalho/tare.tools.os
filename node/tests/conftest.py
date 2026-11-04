import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Tests run tare_node modules as scripts in child processes: they import the package from the checkout.
os.environ['PYTHONPATH'] = os.pathsep.join([str(ROOT), *filter(None, [os.environ.get('PYTHONPATH')])])
