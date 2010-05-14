from pylons import tmpl_context as c
from authorization import has_permission_bool as has
import proposal


def index(p):
    return proposal.index()
    
def show(s):
    return has('proposal.show') and not s.is_deleted()

def create(p):
    if not p.is_mutable():
        return False
    return has('proposal.edit')
    
def edit(s):
    return False
    
def delete(s):
    return has('proposal.delete') and show(s) and s.proposal.is_mutable()