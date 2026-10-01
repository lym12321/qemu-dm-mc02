# DM-MC02 project configuration for arm-softmmu.
#
# Keep the generic ARM default configuration free of project-specific
# devices.  The project build selects this file with --with-devices-arm.

include default.mak

CONFIG_DM_MC02=y
CONFIG_STM32H723_USB_HOST=y
