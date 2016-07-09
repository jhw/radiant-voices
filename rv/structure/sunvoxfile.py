from .project import Project


class SunvoxFile(object):

    def __init__(self):
        self.project = Project()
        self._modules = []
        self._patterns = []

    @property
    def modules(self):
        mlist = self._modules[:]
        while mlist[-1:] == [None]:
            mlist = mlist[:-1]
        return mlist

    @property
    def patterns(self):
        plist = self._patterns[:]
        while plist[-1:] == [None]:
            plist = plist[:-1]
        return plist

    def __getstate__(self):
        return dict(
            project=self.project,
            modules=self.modules,
            patterns=self.patterns,
        )
