from qgis.utils import iface 

@qgsfunction(args='auto', group='Custom')
def get_composer_map_scale(comp_window_title, feature, parent):
    composer_views = iface.activeComposers()
    my_composition = None
    for view in composer_views:
        if view.window().windowTitle() == comp_window_title:
            my_composition = view.composition()
            break
    if my_composition is not None:
        # adjust map id below if you have more maps in the composer
        comp_map = my_composition.getComposerMapById(0)
        scale = '{:.0f}'.format(round(comp_map.scale(), 0))
        return scale
    else:
        return 'Unknown'
